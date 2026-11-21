"""Enforcement points: the agent-harness hook, the checkpoint and the git hook installer."""

import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from agentsitter import cli, codes, config, gate, lanes, state

from . import helpers

BLOCK = 2  # agent-harness hook protocol: exit 2 blocks and shows stderr to the agent
APPROVE_RE = re.compile(r"agentsitter approve ([0-9a-f]{16})")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.key, public = helpers.make_key(self.tmp, "approver_key")
        self.repo = helpers.make_repo(self.tmp / "repo")
        self.brief = self.tmp / "brief.md"
        self.brief.write_text("Question: is this change set safe to finish?\n", encoding="utf-8")
        override = {
            "approver": {"allowed_signers": str(helpers.trust(self.tmp, public))},
            "advisors": {"gemini": {"model": "gemini-test-model"}},
            "changeset": {"max_files": 3},
            # The test repo lives under the system temp dir, which is scratch space by default.
            "scratch_roots": [],
        }
        self.env = helpers.base_env(self.tmp, config=override, family="claude")
        self.cfg = config.merge(config.defaults(), override)
        self.state = state.for_project(lanes.project_root(self.repo), self.env)

    def tearDown(self):
        self._tmp.cleanup()

    def cli(self, *args, stdin=None):
        return helpers.cli(list(args), self.env, self.repo, stdin=stdin)

    def add_files(self, count):
        for n in range(count):
            (self.repo / f"file{n}.txt").write_text(f"line {n}\n", encoding="utf-8")


class AgentHookTest(Base):
    def hook(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return self.cli("hook", "agent", stdin=text)

    def pre_tool(self, tool, **tool_input):
        return self.hook({
            "session_id": "test-session", "transcript_path": str(self.tmp / "transcript.jsonl"),
            "cwd": str(self.repo), "permission_mode": "default", "hook_event_name": "PreToolUse",
            "tool_name": tool, "tool_input": tool_input,
        })

    def stop(self, active=False):
        return self.hook({
            "session_id": "test-session", "transcript_path": str(self.tmp / "transcript.jsonl"),
            "cwd": str(self.repo), "hook_event_name": "Stop", "stop_hook_active": active,
        })

    def test_a_routine_shell_command_is_allowed(self):
        done = self.pre_tool("Bash", command="git status", description="Show status")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_publish_is_blocked_until_signed_and_then_only_once(self):
        done = self.pre_tool("Bash", command="npm publish")
        self.assertEqual(done.returncode, BLOCK, done.stderr)
        found = APPROVE_RE.search(done.stderr)
        self.assertIsNotNone(found, done.stderr)
        gate.sign(self.state, found.group(1), self.key, self.cfg)
        self.assertEqual(self.pre_tool("Bash", command="npm publish").returncode, 0)
        self.assertEqual(self.pre_tool("Bash", command="npm publish").returncode, BLOCK)

    def test_a_wrapped_publish_is_blocked_with_an_instruction_to_run_it_directly(self):
        done = self.pre_tool("Bash", command="agentsitter run -- npm publish")
        self.assertEqual(done.returncode, BLOCK, done.stderr)
        self.assertIn("without the `agentsitter run --` wrapper", done.stderr)
        self.assertIsNone(APPROVE_RE.search(done.stderr), done.stderr)
        self.assertEqual(gate.pending(self.state), [])

    def test_a_fake_wrapper_prefix_does_not_wave_a_publish_through(self):
        done = self.pre_tool("Bash", command="./python -m agentsitter run -- npm publish")
        self.assertEqual(done.returncode, BLOCK, done.stderr)

    def test_a_wrapped_routine_command_is_allowed(self):
        done = self.pre_tool("Bash", command="agentsitter run -- git status")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_dependency_change_is_blocked_with_the_consult_command(self):
        done = self.pre_tool("Bash", command="npm install left-pad")
        self.assertEqual(done.returncode, BLOCK, done.stderr)
        self.assertIn("agentsitter consult --decision", done.stderr)

    def test_a_powershell_deletion_outside_scratch_is_blocked(self):
        done = self.pre_tool("PowerShell", command="Remove-Item -Recurse -Force src")
        self.assertEqual(done.returncode, BLOCK, done.stderr)
        self.assertRegex(done.stderr, APPROVE_RE)

    def test_writing_a_secrets_file_is_blocked(self):
        done = self.pre_tool("Write", file_path=str(self.repo / ".env"), content="TOKEN=x\n")
        self.assertEqual(done.returncode, BLOCK, done.stderr)
        self.assertRegex(done.stderr, APPROVE_RE)

    def test_writing_ordinary_source_is_allowed(self):
        done = self.pre_tool("Write", file_path=str(self.repo / "src" / "app.py"), content="print(1)\n")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_writing_an_env_template_is_allowed(self):
        done = self.pre_tool("Write", file_path=str(self.repo / ".env.example"), content="TOKEN=\n")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_editing_a_git_hook_is_blocked(self):
        done = self.pre_tool("Edit", file_path=str(self.repo / ".git" / "hooks" / "pre-commit"),
                             old_string="exit 1", new_string="exit 0")
        self.assertEqual(done.returncode, BLOCK, done.stderr)
        self.assertRegex(done.stderr, APPROVE_RE)

    def test_editing_the_harness_settings_that_install_this_hook_is_blocked(self):
        for name in ("settings.json", "settings.local.json"):
            with self.subTest(name=name):
                done = self.pre_tool("Edit", file_path=str(self.repo / ".claude" / name),
                                     old_string="agentsitter hook agent", new_string="true")
                self.assertEqual(done.returncode, BLOCK, done.stderr)
                self.assertRegex(done.stderr, APPROVE_RE)

    def test_a_shell_command_that_names_the_harness_settings_is_blocked(self):
        done = self.pre_tool("Bash", command="sed -i s/agentsitter/true/ .claude/settings.json")
        self.assertEqual(done.returncode, BLOCK, done.stderr)
        self.assertRegex(done.stderr, APPROVE_RE)

    def test_a_protected_path_from_the_config_is_blocked(self):
        self.env = helpers.base_env(self.tmp, config={"protected_paths": [r"\.harness/hooks\.toml$"]})
        done = self.pre_tool("Write", file_path=str(self.repo / ".harness" / "hooks.toml"), content="")
        self.assertEqual(done.returncode, BLOCK, done.stderr)

    def test_writing_the_approver_folders_is_blocked_under_either_name(self):
        # agent-babysitter is the pre-0.2.0 name; its keys may still be on disk.
        for folder in ("agentsitter", "agent-babysitter"):
            for path in (self.tmp / "home" / ".config" / folder / "config.json",
                         self.tmp / "home" / ".local" / "state" / folder / "ledger.json"):
                with self.subTest(path=path):
                    done = self.pre_tool("Write", file_path=str(path), content="{}")
                    self.assertEqual(done.returncode, BLOCK, done.stderr)
                    self.assertRegex(done.stderr, APPROVE_RE)

    def test_editing_this_package_is_blocked(self):
        from agentsitter import hooks as running
        done = self.pre_tool("Edit", file_path=running.__file__, old_string="return 2", new_string="return 0")
        self.assertEqual(done.returncode, BLOCK, done.stderr)
        self.assertRegex(done.stderr, APPROVE_RE)

    def test_a_shell_command_that_names_this_package_is_blocked(self):
        from agentsitter import hooks as running
        done = self.pre_tool("Bash", command="sed -i s/2/0/ " + Path(running.__file__).as_posix())
        self.assertEqual(done.returncode, BLOCK, done.stderr)

    def test_an_edit_without_a_usable_target_fails_closed(self):
        for tool_input in ({}, {"file_path": 42}, {"file_path": ""}, {"content": "x"}):
            with self.subTest(tool_input=tool_input):
                done = self.pre_tool("Write", **tool_input)
                self.assertEqual(done.returncode, BLOCK, done.stderr)

    def test_tool_input_that_is_not_an_object_fails_closed(self):
        for tool in ("Write", "Bash"):
            with self.subTest(tool=tool):
                done = self.hook({"cwd": str(self.repo), "hook_event_name": "PreToolUse",
                                  "tool_name": tool, "tool_input": [1]})
                self.assertEqual(done.returncode, BLOCK, done.stderr)

    def test_a_checkpoint_that_cannot_read_the_change_set_fails_closed(self):
        self.add_files(1)
        (self.repo / ".git" / "index").write_bytes(b"not an index")
        self.assertEqual(self.stop().returncode, BLOCK)
        self.assertEqual(self.cli("checkpoint").returncode, codes.USAGE)

    def test_the_approver_is_shown_the_edit_itself_not_only_its_hash(self):
        done = self.pre_tool("Write", file_path=str(self.repo / ".env"), content="TOKEN=hunter2\n")
        request_id = APPROVE_RE.search(done.stderr).group(1)
        shown = io.StringIO()
        code = cli.approve_request(self.state, self.cfg, request_id, self.key,
                                   confirm=lambda prompt: "n", out=shown, err=shown)
        self.assertEqual(code, codes.APPROVE_REFUSED)
        self.assertIn("TOKEN=hunter2", shown.getvalue())

    def test_a_request_whose_edit_was_swapped_after_the_fact_is_not_signed(self):
        done = self.pre_tool("Write", file_path=str(self.repo / ".env"), content="TOKEN=hunter2\n")
        request_id = APPROVE_RE.search(done.stderr).group(1)
        held = self.state.approvals / f"{request_id}.request.json"
        for change in ("swap", "drop"):
            with self.subTest(change=change):
                document = json.loads(held.read_text(encoding="utf-8"))
                if change == "swap":
                    document["detail"]["content"] = "harmless\n"
                else:
                    del document["detail"]
                held.write_bytes(gate.canonical(document))
                shown = io.StringIO()
                code = cli.approve_request(self.state, self.cfg, request_id, self.key,
                                           confirm=lambda prompt: "y", out=shown, err=shown)
                self.assertEqual(code, codes.APPROVAL_INVALID, shown.getvalue())
                self.assertEqual(list(self.state.approvals.glob("*.sig")), [])

    def test_a_protected_file_reached_through_a_link_is_still_protected(self):
        link = self.tmp / "alias"
        try:
            os.symlink(self.repo / ".git", link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this account cannot create symbolic links")
        done = self.pre_tool("Write", file_path=str(link / "hooks" / "pre-commit"), content="exit 0\n")
        self.assertEqual(done.returncode, BLOCK, done.stderr)

    def test_a_config_of_the_wrong_shape_is_a_clear_error_and_blocks(self):
        self.env = helpers.base_env(self.tmp, config={"budget": None, "family_markers": None})
        status = self.cli("status")
        self.assertEqual(status.returncode, codes.USAGE, status.stderr)
        self.assertNotIn("Traceback", status.stderr)
        self.assertEqual(self.pre_tool("Bash", command="git status").returncode, BLOCK)

    def test_every_path_an_edit_names_is_checked(self):
        done = self.pre_tool("NotebookEdit", file_path=str(self.repo / "ok.txt"),
                             notebook_path=str(self.repo / ".env"), new_source="x")
        self.assertEqual(done.returncode, BLOCK, done.stderr)

    def test_malformed_input_fails_closed(self):
        done = self.hook("{not json")
        self.assertEqual(done.returncode, BLOCK, done.stderr)
        self.assertIn("not valid JSON", done.stderr)

    def test_stopping_with_a_large_change_set_requires_a_consult(self):
        self.add_files(4)
        done = self.stop()
        self.assertEqual(done.returncode, BLOCK, done.stderr)
        self.assertIn("agentsitter consult --decision changeset:", done.stderr)

    def test_a_stop_that_is_already_continuing_is_not_blocked_again(self):
        self.add_files(4)
        self.assertEqual(self.stop(active=True).returncode, 0)

    def test_stopping_with_a_small_change_is_allowed(self):
        self.add_files(1)
        done = self.stop()
        self.assertEqual(done.returncode, 0, done.stderr)


class CheckpointTest(Base):
    def test_a_small_change_is_routine(self):
        self.add_files(1)
        done = self.cli("checkpoint")
        self.assertEqual(done.returncode, codes.OK, done.stderr)

    def test_a_large_change_needs_a_consult_and_then_passes(self):
        self.add_files(3)  # one staged, two untracked, plus one tracked edit: 4 files > max_files 3
        (self.repo / "README.md").write_text("seed\nmore\n", encoding="utf-8")
        helpers.git(self.repo, "add", "file0.txt")
        index_before = (self.repo / ".git" / "index").read_bytes()

        done = self.cli("checkpoint", "--json")
        self.assertEqual(done.returncode, codes.CONSULT_REQUIRED, done.stderr)
        info = json.loads(done.stdout)
        self.assertTrue(info["decision"].startswith("changeset:"), info)
        self.assertEqual(info["seat"], "code")
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index_before)

        consulted = self.cli("consult", "--decision", info["decision"], "--seat", "code",
                             "--brief-file", str(self.brief))
        self.assertEqual(consulted.returncode, codes.OK, consulted.stderr)
        done = self.cli("checkpoint", "--json")
        self.assertEqual(done.returncode, codes.OK, done.stderr)
        self.assertEqual(json.loads(done.stdout)["status"], "consulted")


class InstallHooksTest(Base):
    def test_a_foreign_hook_is_not_overwritten_without_force(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

        refused = self.cli("install-hooks")
        self.assertEqual(refused.returncode, codes.USAGE, refused.stderr)
        self.assertEqual(hook.read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")

        forced = self.cli("install-hooks", "--force")
        self.assertEqual(forced.returncode, codes.OK, forced.stderr)
        self.assertIn("agentsitter hook", hook.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
