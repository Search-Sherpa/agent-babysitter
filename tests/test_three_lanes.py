"""The three-lane governance test.

One temp repository, one trusted approver key, our git hooks installed. Each
test reads as one clause of the lane contract:

  Lane 1  routine commands run.
  Lane 2  non-trivial commands need one cross-family advisor consult on record.
  Lane 3  irreversible commands need a signed human approval bound to the action.

The tests play the human: they sign with gate.sign directly, using a key the
CLI never sees.
"""

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agentsitter import cli, codes, config, consult, gate, lanes, state

from . import helpers

ADVISOR_STUBS = {"claude", "agy", "codex", "grok"}


class Governed(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.key, public = helpers.make_key(self.tmp, "approver_key")
        self.anchor = helpers.trust(self.tmp, public)
        self.repo = helpers.make_repo(self.tmp / "repo")
        self.brief = self.tmp / "brief.md"
        self.brief.write_text("Question: add this dependency or write the helper ourselves?\n", encoding="utf-8")
        self.configure()
        done = self.cli("install-hooks")
        self.assertEqual(done.returncode, codes.OK, done.stderr)

    def tearDown(self):
        self._tmp.cleanup()

    def configure(self, override=None, stub_mode="verdict"):
        self.override = config.merge({
            "approver": {"allowed_signers": str(self.anchor)},
            "advisors": {"gemini": {"model": "gemini-test-model"}},
        }, override or {})
        self.env = helpers.base_env(self.tmp, config=self.override, family="claude", stub_mode=stub_mode)
        self.cfg = config.merge(config.defaults(), self.override)
        self.state = state.for_project(lanes.project_root(self.repo), self.env)

    # ---- the agent's side ---------------------------------------------------------
    def cli(self, *args, env=None, stdin=None, cwd=None):
        return helpers.cli(list(args), env or self.env, cwd or self.repo, stdin=stdin)

    def check(self, command, cwd=None):
        # One argument is taken as a shell command line, exactly as written.
        done = self.cli("check", "--json", "--", command, cwd=cwd)
        return json.loads(done.stdout), done.returncode

    def consult_for(self, command):
        info, code = self.check(command)
        self.assertEqual(code, codes.CONSULT_REQUIRED, info)
        return self.cli("consult", "--decision", info["decision"], "--seat", info["seat"],
                        "--brief-file", str(self.brief))

    def git(self, *args):
        return helpers.git(self.repo, *args, env=self.env, check=False)

    def head(self):
        return helpers.git(self.repo, "rev-parse", "HEAD").stdout.strip()

    # ---- the human's side ---------------------------------------------------------
    def requests(self, kind=None):
        done = self.cli("requests", "--json")
        self.assertEqual(done.returncode, codes.OK, done.stderr)
        return [r for r in json.loads(done.stdout) if kind is None or r["kind"] == kind]

    def approve(self, request_id):
        gate.sign(self.state, request_id, self.key, self.cfg)

    # ---- what actually ran --------------------------------------------------------
    def calls(self):
        log = self.tmp / "stub-calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []

    def advisor_calls(self):
        return [c for c in self.calls() if c in ADVISOR_STUBS]

    def npm_runs(self, args):
        return self.calls().count("npm " + args)


class Lane1RoutineCommandsRun(Governed):
    def test_lane_1_a_routine_command_runs_with_no_consult_and_no_approval(self):
        done = self.cli("run", "--", "git", "status")
        self.assertEqual(done.returncode, codes.OK, done.stderr)
        self.assertEqual(self.advisor_calls(), [])
        self.assertEqual(self.requests(), [])


class Lane2NeedsOneCrossFamilyConsult(Governed):
    def test_lane_2_a_dependency_change_waits_for_a_consult_from_another_family(self):
        held = self.cli("run", "--", "npm", "install", "left-pad")
        self.assertEqual(held.returncode, codes.CONSULT_REQUIRED, held.stderr)
        self.assertIn("agentsitter consult --decision", held.stderr)
        self.assertEqual(self.npm_runs("install left-pad"), 0)

        consulted = self.consult_for("npm install left-pad")
        self.assertEqual(consulted.returncode, codes.OK, consulted.stderr)
        # Builder is claude, the seat is architecture: the gemini CLI (agy) answers.
        self.assertEqual(self.advisor_calls(), ["agy"])

        ran = self.cli("run", "--", "npm", "install", "left-pad")
        self.assertEqual(ran.returncode, codes.OK, ran.stderr)
        self.assertEqual(self.npm_runs("install left-pad"), 1)

    def test_lane_2_two_failed_consults_let_the_builder_proceed_on_record(self):
        self.configure(stub_mode="narration")
        for _ in range(2):
            self.assertEqual(self.consult_for("npm install left-pad").returncode, codes.CONSULT_FAILED)
        info, code = self.check("npm install left-pad")
        self.assertEqual((code, info["status"]), (codes.OK, "consult-fallback"))
        ran = self.cli("run", "--", "npm", "install", "left-pad")
        self.assertEqual(ran.returncode, codes.OK, ran.stderr)
        self.assertEqual(self.npm_runs("install left-pad"), 1)

    def test_lane_2_an_exhausted_budget_stops_the_builder_until_the_approver_resets_it(self):
        self.configure({"budget": {"per_window": 1}})
        self.assertEqual(self.consult_for("npm install left-pad").returncode, codes.OK)

        held = self.cli("run", "--", "npm", "install", "right-pad")
        self.assertEqual(held.returncode, codes.BUDGET_EXHAUSTED, held.stderr)
        self.assertIn("agentsitter budget reset", held.stderr)
        self.assertEqual(self.npm_runs("install right-pad"), 0)

        reset = self.cli("budget", "reset")
        self.assertEqual(reset.returncode, codes.APPROVAL_REQUIRED, reset.stderr)
        [request] = self.requests("budget-reset")
        self.approve(request["id"])
        reset = self.cli("budget", "reset")
        self.assertEqual(reset.returncode, codes.OK, reset.stderr)

        self.assertEqual(self.consult_for("npm install right-pad").returncode, codes.OK)
        ran = self.cli("run", "--", "npm", "install", "right-pad")
        self.assertEqual(ran.returncode, codes.OK, ran.stderr)
        self.assertEqual(self.npm_runs("install right-pad"), 1)


class Lane3CommitNeedsASignedApproval(Governed):
    def stage(self, name, text):
        (self.repo / name).write_text(text, encoding="utf-8")
        helpers.git(self.repo, "add", name)

    def commit(self):
        return self.git("commit", "-q", "-m", "change")

    def test_lane_3_a_commit_is_blocked_without_a_signed_approval(self):
        before = self.head()
        self.stage("a.txt", "A\n")
        done = self.commit()
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.head(), before)
        [request] = self.requests("git-commit")
        self.assertIn("agentsitter approve " + request["id"], done.stderr)

    def test_lane_3_a_look_alike_approval_with_a_garbage_signature_does_not_pass(self):
        before = self.head()
        self.stage("a.txt", "A\n")
        self.commit()
        [request] = self.requests("git-commit")
        held = json.loads((self.state.approvals / f"{request['id']}.request.json").read_text(encoding="utf-8"))
        now = int(time.time())
        forged = {"v": 1, "action": held["action"], "digest": held["digest"], "nonce": "00" * 16,
                  "approved_at": now, "expires_at": now + 60, "approver": "approver"}
        approval = self.state.approvals / f"{request['id']}.approval.json"
        approval.write_bytes(gate.canonical(forged))
        Path(str(approval) + ".sig").write_text(
            "-----BEGIN SSH SIGNATURE-----\nbm90IGEgc2lnbmF0dXJl\n-----END SSH SIGNATURE-----\n", encoding="utf-8")
        done = self.commit()
        self.assertNotEqual(done.returncode, 0, "a forged approval let the commit through")
        self.assertEqual(self.head(), before)

    def test_lane_3_a_signed_approval_lets_that_commit_through(self):
        before = self.head()
        self.stage("a.txt", "A\n")
        self.assertNotEqual(self.commit().returncode, 0)
        [request] = self.requests("git-commit")
        self.approve(request["id"])
        done = self.commit()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotEqual(self.head(), before)
        self.assertEqual(helpers.git(self.repo, "rev-parse", "HEAD~1").stdout.strip(), before)

    def test_lane_3_an_approval_for_a_plain_commit_does_not_pass_a_merge_of_the_same_tree(self):
        side = helpers.git(self.repo, "commit-tree", "HEAD^{tree}", "-m", "side").stdout.strip()
        before = self.head()
        self.stage("a.txt", "A\n")
        self.commit()
        [request] = self.requests("git-commit")
        self.approve(request["id"])
        (self.repo / ".git" / "MERGE_HEAD").write_bytes(side.encode("ascii") + b"\n")
        done = self.commit()
        self.assertNotEqual(done.returncode, 0, "the approval for a plain commit let a merge through")
        self.assertEqual(self.head(), before)
        self.assertIn("needs a signed human approval", done.stderr)

    def test_lane_3_an_approval_for_staged_content_a_does_not_pass_content_b(self):
        before = self.head()
        self.stage("a.txt", "A\n")
        self.commit()
        [request] = self.requests("git-commit")
        self.approve(request["id"])
        self.stage("a.txt", "B\n")
        done = self.commit()
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.head(), before)


class TheGitHookRunsOnlyItsOwnCode(Governed):
    """The hook starts in the work tree, which the governed agent writes all day.
    Nothing there, and nothing named by a Python variable, may stand in for the
    standard library or for this package."""

    PLANT = "raise SystemExit(0)\n"

    def commit_is_blocked(self, env=None):
        before = self.head()
        (self.repo / "a.txt").write_text("A\n", encoding="utf-8")
        helpers.git(self.repo, "add", "-A")
        done = helpers.git(self.repo, "commit", "-q", "-m", "change", env=env or self.env, check=False)
        self.assertNotEqual(done.returncode, 0, "the commit went through without an approval")
        self.assertEqual(self.head(), before)
        self.assertIn("needs a signed human approval", done.stderr)

    def test_a_standard_library_name_planted_in_the_work_tree_is_not_imported(self):
        for name in ("argparse.py", "json.py", "subprocess.py"):
            (self.repo / name).write_text(self.PLANT, encoding="utf-8")
        self.commit_is_blocked()

    def test_a_package_of_our_name_planted_in_the_work_tree_is_not_imported(self):
        fake = self.repo / "agentsitter"
        fake.mkdir()
        (fake / "__init__.py").write_text("", encoding="utf-8")
        (fake / "cli.py").write_text("def main(argv=None):\n    return 0\n", encoding="utf-8")
        self.commit_is_blocked()

    def test_a_module_named_by_PYTHONPATH_is_not_imported(self):
        planted = self.tmp / "planted"
        planted.mkdir()
        (planted / "argparse.py").write_text(self.PLANT, encoding="utf-8")
        self.commit_is_blocked(env=dict(self.env, PYTHONPATH=str(planted)))

    def test_hooks_work_when_the_install_path_holds_quotes_and_spaces(self):
        # The shim names the interpreter and the package location. Neither may be
        # read as shell or as Python source.
        from agentsitter import hooks
        for name in hooks.HOOK_NAMES:
            with self.subTest(hook=name):
                text = hooks.shim(name, python="/opt/it's a \"py\" $HOME `x`/python",
                                  parent="/srv/o'brien \"pkg\" $(touch pwned)/src")
                script = self.tmp / "shim.sh"
                # open() rather than Path.write_text: its newline argument needs Python 3.10.
                with open(script, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(text.replace("exec ", "printf '%s\\n' ", 1))
                # find_bash, not PATH: a Windows shell need not have bash on PATH.
                bash = consult.find_bash(config.defaults(), os.environ)
                done = helpers.subprocess.run([bash, str(script), "arg one"], capture_output=True, text=True,
                                              cwd=str(self.tmp))
                self.assertEqual(done.returncode, 0, done.stderr)
                words = done.stdout.splitlines()
                self.assertEqual(words[0], "/opt/it's a \"py\" $HOME `x`/python")
                # -I: no current directory, no PYTHON* variables. -S: no site-packages
                # start-up files (.pth, sitecustomize), which an agent could write.
                self.assertEqual(words[1:3], ["-I", "-S"])
                self.assertIn("/srv/o'brien \"pkg\" $(touch pwned)/src", words)
                self.assertEqual(words[-2:], [name, "arg one"])
                self.assertFalse((self.tmp / "pwned").exists())


class Lane3PushNeedsASignedApproval(Governed):
    def test_lane_3_a_push_is_blocked_until_that_push_is_approved(self):
        remote = self.tmp / "remote.git"
        helpers.git(self.tmp, "init", "-q", "--bare", str(remote))
        helpers.git(self.repo, "remote", "add", "origin", str(remote))

        def remote_head():
            done = helpers.git(remote, "rev-parse", "-q", "--verify", "refs/heads/main", check=False)
            return done.stdout.strip() if done.returncode == 0 else None

        blocked = self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIsNone(remote_head())
        [request] = self.requests("git-push")
        self.approve(request["id"])
        done = self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(remote_head(), self.head())


class Lane3HistoryRewritesNeedASignedApproval(Governed):
    """The pre-rebase and pre-merge-commit hooks, driven through real git."""

    def setUp(self):
        super().setUp()
        helpers.git(self.repo, "branch", "-M", "main")

    def human_commit(self, name, text):
        # Fixture history is written by the human, past our hooks.
        (self.repo / name).write_text(text, encoding="utf-8")
        helpers.git(self.repo, "add", name)
        helpers.git(self.repo, "commit", "-q", "--no-verify", "-m", f"add {name}")

    def rev(self, ref):
        return helpers.git(self.repo, "rev-parse", ref).stdout.strip()

    def diverge(self, branch, branch_file, main_file):
        helpers.git(self.repo, "checkout", "-q", "-b", branch)
        self.human_commit(branch_file, "branch\n")
        helpers.git(self.repo, "checkout", "-q", "main")
        self.human_commit(main_file, "main\n")

    def test_lane_3_a_rebase_is_blocked_until_that_rebase_is_approved(self):
        self.diverge("feature", "f.txt", "m.txt")
        helpers.git(self.repo, "checkout", "-q", "feature")
        before = self.rev("feature")

        blocked = self.git("rebase", "main")
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertEqual(self.rev("feature"), before)
        [request] = self.requests("git-rebase")
        self.assertIn("agentsitter approve " + request["id"], blocked.stderr)

        self.approve(request["id"])
        done = self.git("rebase", "main")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.rev("feature~1"), self.rev("main"))
        self.assertNotEqual(self.rev("feature"), before)

    def test_lane_3_a_rebase_approval_does_not_pass_once_the_upstream_has_moved(self):
        # The approval names commits, not branch names that can be pointed elsewhere.
        self.diverge("feature", "f.txt", "m.txt")
        helpers.git(self.repo, "checkout", "-q", "feature")
        before = self.rev("feature")
        self.git("rebase", "main")
        [request] = self.requests("git-rebase")
        self.approve(request["id"])
        helpers.git(self.repo, "checkout", "-q", "main")
        self.human_commit("later.txt", "main moved on\n")
        helpers.git(self.repo, "checkout", "-q", "feature")
        done = self.git("rebase", "main")
        self.assertNotEqual(done.returncode, 0, "an approval for one upstream passed for another")
        self.assertEqual(self.rev("feature"), before)

    def test_lane_3_a_merge_commit_is_blocked_until_that_merge_is_approved(self):
        self.diverge("other", "o.txt", "m.txt")
        before = self.rev("main")

        request_id = self.blocked_merge(before)

        # git leaves the merge staged with MERGE_HEAD set, and refuses a second
        # `git merge` until it is concluded. Abort it, then retry the same merge.
        self.assertEqual(self.git("merge", "--abort").returncode, 0)
        self.approve(request_id)
        done = self.git("merge", "--no-ff", "--no-edit", "other")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.rev("main^1"), before)
        self.assertEqual(self.rev("main^2"), self.rev("other"))

    def blocked_merge(self, before):
        blocked = self.git("merge", "--no-ff", "--no-edit", "other")
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertEqual(self.rev("main"), before)
        [request] = self.requests("git-merge-commit")
        self.assertIn("agentsitter approve " + request["id"], blocked.stderr)
        action = gate.load_request(self.state, request["id"])["action"]
        self.assertEqual(action["merge_head"], self.rev("other"), "the approval must name the merged commit")
        self.assertTrue((self.repo / ".git" / "MERGE_HEAD").is_file())
        return request["id"]

    def test_lane_3_concluding_a_blocked_merge_with_commit_needs_a_commit_approval(self):
        self.diverge("other", "o.txt", "m.txt")
        before = self.rev("main")
        merge_request = self.blocked_merge(before)
        self.approve(merge_request)

        # `git commit` concludes a merge through pre-commit, not pre-merge-commit,
        # so the merge approval does not cover it.
        held = self.git("commit", "--no-edit")
        self.assertNotEqual(held.returncode, 0, held.stdout + held.stderr)
        self.assertEqual(self.rev("main"), before)
        [commit_request] = self.requests("git-commit")

        self.approve(commit_request["id"])
        done = self.git("commit", "--no-edit")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.rev("main^1"), before)
        self.assertEqual(self.rev("main^2"), self.rev("other"))


class Lane3CommandNeedsASignedApproval(Governed):
    def test_lane_3_a_publish_runs_once_per_signed_approval(self):
        held = self.cli("run", "--", "npm", "publish")
        self.assertEqual(held.returncode, codes.APPROVAL_REQUIRED, held.stderr)
        self.assertEqual(self.npm_runs("publish"), 0)
        [request] = self.requests("command")
        self.assertIn("agentsitter approve " + request["id"], held.stderr)

        self.approve(request["id"])
        ran = self.cli("run", "--", "npm", "publish")
        self.assertEqual(ran.returncode, codes.OK, ran.stderr)
        self.assertEqual(self.npm_runs("publish"), 1)

        again = self.cli("run", "--", "npm", "publish")
        self.assertEqual(again.returncode, codes.APPROVAL_REQUIRED, again.stderr)
        self.assertEqual(self.npm_runs("publish"), 1)


class RunSpendsAnApprovalOnlyWhenItCanRun(Governed):
    def test_a_bash_that_cannot_start_leaves_the_approval_unspent(self):
        self.assertEqual(self.cli("run", "--", "npm", "publish").returncode, codes.APPROVAL_REQUIRED)
        [request] = self.requests("command")
        self.approve(request["id"])
        not_a_program = self.tmp / "not-bash.txt"
        not_a_program.write_text("this is not a program\n", encoding="utf-8")
        for label, path in (("missing", self.tmp / "no-such-bash"), ("not a program", not_a_program),
                            ("a directory", self.tmp)):
            with self.subTest(bash=label):
                done = self.cli("run", "--", "npm", "publish", env=dict(self.env, AGENTSITTER_BASH=str(path)))
                self.assertEqual(done.returncode, codes.USAGE, done.stderr)
                self.assertIn("bash", done.stderr)
                self.assertNotIn("Traceback", done.stderr)
                self.assertEqual(self.npm_runs("publish"), 0)
        ran = self.cli("run", "--", "npm", "publish")
        self.assertEqual(ran.returncode, 0, "the approval was still there to use: " + ran.stderr)
        self.assertEqual(self.npm_runs("publish"), 1)
        self.assertEqual(self.cli("run", "--", "npm", "publish").returncode, codes.APPROVAL_REQUIRED)

    def test_a_blocked_command_is_still_reported_as_blocked_when_bash_is_missing(self):
        done = self.cli("run", "--", "npm", "publish", env=dict(self.env, AGENTSITTER_BASH=str(self.tmp / "none")))
        self.assertEqual(done.returncode, codes.APPROVAL_REQUIRED, done.stderr)


class RunKeepsTheCommandAsGiven(Governed):
    def run_words(self, *words):
        done = self.cli("run", "--", *words)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_several_arguments_stay_separate_words(self):
        self.assertEqual(self.run_words("printf", "%s|", "a b", "c;d", "$HOME", "it's"), "a b|c;d|$HOME|it's|")

    def test_one_argument_is_a_shell_command_line(self):
        self.assertEqual(self.run_words("printf '%s|' 'a b' && printf done"), "a b|done")

    def test_a_shell_operator_given_as_its_own_argument_still_joins_commands(self):
        self.assertEqual(self.run_words("printf", "one", "&&", "printf", "two"), "onetwo")

    def test_a_quoted_argument_is_classified_as_one_word(self):
        info, code = self.check("git status")
        self.assertEqual(code, codes.OK, info)
        done = self.cli("check", "--json", "--", "echo", "git push && npm publish")
        self.assertEqual(json.loads(done.stdout)["status"], "approval-required", "words are matched as text")


class DeferralAndBypass(Governed):
    def test_a_commit_is_left_to_our_installed_git_hook(self):
        info, code = self.check("git commit -m x")
        self.assertEqual((code, info["status"]), (codes.OK, "deferred-to-git-hook"))

    def test_skipping_the_git_hook_is_itself_lane_3(self):
        info, code = self.check("git commit --no-verify -m x")
        self.assertEqual((code, info["status"]), (codes.APPROVAL_REQUIRED, "approval-required"))

    def test_without_our_hooks_a_commit_needs_an_approval_up_front(self):
        plain = helpers.make_repo(self.tmp / "plain")
        done = self.cli("check", "--", "git", "commit", "-m", "x", cwd=plain)
        self.assertEqual(done.returncode, codes.APPROVAL_REQUIRED, done.stdout + done.stderr)

    # Our hook guards only the repository that contains cwd. A commit aimed at
    # another repository, which may have no hooks at all, is not deferred.
    def assert_not_deferred(self, command):
        helpers.make_repo(self.tmp / "other")
        info, code = self.check(command)
        self.assertEqual((code, info["status"]), (codes.APPROVAL_REQUIRED, "approval-required"), info)

    def test_a_commit_in_another_repo_via_dash_C_is_not_deferred(self):
        self.assert_not_deferred("git -C ../other commit -m x")

    def test_a_commit_after_changing_directory_is_not_deferred(self):
        self.assert_not_deferred("cd ../other && git commit -m x")

    def test_a_commit_with_an_explicit_git_dir_is_not_deferred(self):
        self.assert_not_deferred("git --git-dir=../other/.git commit -m x")

    def test_a_commit_with_a_GIT_variable_assignment_is_not_deferred(self):
        self.assert_not_deferred("GIT_DIR=../other/.git git commit -m x")

    def test_a_commit_with_a_config_override_is_not_deferred(self):
        self.assert_not_deferred("git -c user.name=x commit -m x")

    # The git hook runs in the environment the command gives it. It finds its
    # config, trust anchor and state through HOME, its tools through PATH and its
    # own modules through Python's variables, so a commit that sets any variable
    # is not left to the hook alone.
    def test_a_commit_with_a_redirected_home_is_not_deferred(self):
        self.assert_not_deferred("HOME=../other git commit -m x")

    def test_a_commit_with_a_redirected_user_profile_is_not_deferred(self):
        self.assert_not_deferred("USERPROFILE=../other git commit -m x")

    def test_a_commit_with_a_redirected_path_is_not_deferred(self):
        self.assert_not_deferred("PATH=../other git commit -m x")

    def test_a_commit_with_a_python_path_is_not_deferred(self):
        self.assert_not_deferred("PYTHONPATH=../other git commit -m x")

    def test_a_commit_after_an_exported_variable_is_not_deferred(self):
        self.assert_not_deferred("export HOME=../other; git commit -m x")

    def test_a_commit_under_env_with_an_assignment_is_not_deferred(self):
        self.assert_not_deferred("env HOME=../other git commit -m x")

    def test_a_powershell_commit_after_an_env_assignment_is_not_deferred(self):
        self.assert_not_deferred("$env:HOME='../other'; git commit -m x")

    def test_a_commit_with_config_taken_from_the_environment_is_not_deferred(self):
        self.assert_not_deferred("git --config-env=user.name=PATH commit -m x")

    def test_a_commit_with_any_other_global_git_option_is_not_deferred(self):
        self.assert_not_deferred("git --namespace=other commit -m x")

    def test_an_option_after_the_subcommand_does_not_stop_the_deferral(self):
        info, code = self.check("git commit -C HEAD")
        self.assertEqual((code, info["status"]), (codes.OK, "deferred-to-git-hook"), info)

    # The pre-rebase hook is told the upstream and the branch, never --onto, so a
    # rebase onto another base is approved as the full command instead.
    def test_a_rebase_onto_another_base_is_not_deferred(self):
        self.assert_not_deferred("git rebase --onto HEAD~1 HEAD~1 topic")

    # Found by the third review pass.
    def test_a_commit_run_through_exec_is_not_deferred(self):
        # `exec -c` clears the environment, so git may not find the hook we checked.
        self.assert_not_deferred("exec -c git commit -m x")

    def test_a_commit_behind_any_wrapper_that_can_change_how_git_runs_is_not_deferred(self):
        for index, command in enumerate(("command git commit -m x", "env git commit -m x",
                                         "nohup git commit -m x", "xargs git commit -m x")):
            with self.subTest(command=command):
                info, code = self.check(command)
                self.assertEqual((code, info["status"]), (codes.APPROVAL_REQUIRED, "approval-required"), info)

    def test_an_amend_is_not_deferred(self):
        # The hook sees the same tree and HEAD as for a new commit, but an amend replaces HEAD.
        for command in ("git commit --amend -m x", "git commit --ame -m x", "git commit --amend --no-edit"):
            with self.subTest(command=command):
                info, code = self.check(command)
                self.assertEqual((code, info["status"]), (codes.APPROVAL_REQUIRED, "approval-required"), info)

    def test_a_commit_through_a_plain_output_wrapper_is_still_deferred(self):
        for command in ("rtk git commit -m x", "time git commit -m x"):
            with self.subTest(command=command):
                info, code = self.check(command)
                self.assertEqual((code, info["status"]), (codes.OK, "deferred-to-git-hook"), info)

    def blocked(self, command):
        info, code = self.check(command)
        self.assertEqual((code, info["status"]), (codes.APPROVAL_REQUIRED, "approval-required"), info)

    def test_a_shell_function_named_git_is_not_left_to_the_hook(self):
        self.blocked('git() { command git "$@" --no-verify; }; git commit -m x')

    def test_a_commit_beside_anything_but_plain_git_commands_is_not_deferred(self):
        for command in ('eval "cd ../other" && git commit -m x', "alias git=true; git commit -m x",
                        "${env:GIT_CONFIG_GLOBAL}='./gcfg'; git commit -m x", "read HOME < f; git commit -m x"):
            with self.subTest(command=command):
                self.blocked(command)

    def test_a_commit_after_other_plain_git_commands_is_still_deferred(self):
        info, code = self.check("git add -A && git commit -m x")
        self.assertEqual((code, info["status"]), (codes.OK, "deferred-to-git-hook"), info)

    def test_an_abbreviated_no_verify_is_lane_3(self):
        for command in ("git commit --no-veri -m x", "git push --no-v origin main"):
            with self.subTest(command=command):
                self.blocked(command)

    def test_an_option_hidden_by_partial_quotes_is_not_deferred(self):
        for command in ('git commit --""amend -m x', 'git rebase ""--onto main HEAD~1', "git commit --no-''verify -m x",
                        'git commit -""n -m x'):
            with self.subTest(command=command):
                self.blocked(command)

    def test_a_rebase_with_an_abbreviated_onto_is_not_deferred(self):
        self.assert_not_deferred("git rebase --ont=HEAD~1 HEAD~1")

    def test_a_rebase_with_any_option_is_not_deferred(self):
        self.assert_not_deferred("git rebase -i HEAD~1")

    def test_a_commit_after_a_variable_is_removed_is_not_deferred(self):
        self.assert_not_deferred("unset GIT_CONFIG_COUNT; git commit -m x")

    def test_a_powershell_commit_after_a_variable_is_removed_is_not_deferred(self):
        self.assert_not_deferred("Remove-Item env:GIT_CONFIG_COUNT; git commit -m x")

    def test_a_commit_after_a_quoted_cd_is_not_deferred(self):
        self.assert_not_deferred('"cd" ../other && git commit -m x')

    def test_a_plain_rebase_is_left_to_our_installed_git_hook(self):
        info, code = self.check("git rebase main")
        self.assertEqual((code, info["status"]), (codes.OK, "deferred-to-git-hook"), info)

    def test_a_commit_message_that_looks_like_an_assignment_is_still_deferred(self):
        info, code = self.check("git commit -m a=b")
        self.assertEqual((code, info["status"]), (codes.OK, "deferred-to-git-hook"), info)

    # A wrapper that runs a string can aim git anywhere, and the words inside the
    # string are not separate arguments. Only git run directly is left to the hook.
    def test_a_commit_with_dash_C_inside_a_shell_string_is_not_deferred(self):
        self.assert_not_deferred("bash -c 'git -C ../other commit -m x'")

    def test_a_commit_after_cd_inside_a_shell_string_is_not_deferred(self):
        self.assert_not_deferred("sh -c 'cd ../other && git commit -m x'")

    def test_a_commit_with_a_GIT_variable_inside_a_shell_string_is_not_deferred(self):
        self.assert_not_deferred("bash -c 'GIT_DIR=../other/.git git commit -m x'")

    def test_a_commit_inside_eval_is_not_deferred(self):
        self.assert_not_deferred("eval 'git --git-dir=../other/.git commit -m x'")

    # The hook is trusted only when it is exactly the hook this installation
    # writes. A file that merely carries our marker proves nothing.
    def test_a_look_alike_hook_reached_through_hooksPath_is_not_trusted(self):
        fake = self.tmp / "fakehooks"
        fake.mkdir()
        with open(fake / "pre-commit", "w", encoding="utf-8", newline="\n") as handle:
            handle.write("#!/bin/sh\n# agentsitter hook\nexit 0\n")
        (fake / "pre-commit").chmod(0o755)
        helpers.git(self.repo, "config", "core.hooksPath", str(fake))
        info, code = self.check("git commit -m x")
        self.assertEqual((code, info["status"]), (codes.APPROVAL_REQUIRED, "approval-required"), info)

    def test_a_hook_edited_to_do_nothing_is_not_trusted(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        with open(hook, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("exit 0\n")
        info, code = self.check("git commit -m x")
        self.assertEqual((code, info["status"]), (codes.APPROVAL_REQUIRED, "approval-required"), info)

    def test_reinstalling_replaces_a_stale_hook_of_ours_without_force(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        with open(hook, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("#!/bin/sh\n# agentsitter hook (an older release)\nexit 1\n")
        status = self.cli("status")
        self.assertIn("pre-commit", [line for line in status.stdout.splitlines() if "reinstall" in line][0])
        done = self.cli("install-hooks")
        self.assertEqual(done.returncode, codes.OK, done.stderr)
        info, code = self.check("git commit -m x")
        self.assertEqual((code, info["status"]), (codes.OK, "deferred-to-git-hook"), info)

    def test_reinstalling_replaces_a_hook_from_the_release_named_agent_babysitter(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        with open(hook, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("#!/bin/sh\n# agent-babysitter hook (generated by `babysitter install-hooks`)\nexit 1\n")
        # Carrying the old marker earns replacement without --force, never trust.
        info, code = self.check("git commit -m x")
        self.assertEqual((code, info["status"]), (codes.APPROVAL_REQUIRED, "approval-required"), info)
        done = self.cli("install-hooks")
        self.assertEqual(done.returncode, codes.OK, done.stderr)
        info, code = self.check("git commit -m x")
        self.assertEqual((code, info["status"]), (codes.OK, "deferred-to-git-hook"), info)


class SelfApprovalIsRefused(Governed):
    def pending_id(self):
        self.cli("run", "--", "npm", "publish")
        [request] = self.requests("command")
        return request["id"]

    def signatures(self):
        return list(self.state.approvals.glob("*.sig"))

    def test_approve_refuses_inside_an_agent_session(self):
        request_id = self.pending_id()
        done = self.cli("approve", request_id, "--key", str(self.key), env=dict(self.env, CLAUDECODE="1"))
        self.assertEqual(done.returncode, codes.APPROVE_REFUSED, done.stderr)
        self.assertEqual(self.signatures(), [])

    def test_approve_refuses_without_a_terminal(self):
        request_id = self.pending_id()
        done = self.cli("approve", request_id, "--key", str(self.key), stdin="y\n")
        self.assertEqual(done.returncode, codes.APPROVE_REFUSED, done.stderr)
        self.assertEqual(self.signatures(), [])


class ApproverSignsAfterConfirming(Governed):
    """The approver's path past the terminal check: display, prompt, sign."""

    def publish_request(self):
        self.cli("run", "--", "npm", "publish")
        [request] = self.requests("command")
        doc = gate.load_request(self.state, request["id"])
        return request["id"], doc["action"]

    def approve_with(self, request_id, answer, key="default"):
        out, err = io.StringIO(), io.StringIO()
        prompts = []

        def confirm(prompt):
            prompts.append(prompt)
            return answer

        code = cli.approve_request(self.state, self.cfg, request_id, self.key if key == "default" else key,
                                   confirm=confirm, out=out, err=err)
        return code, out.getvalue(), err.getvalue(), prompts

    def signatures(self):
        return list(self.state.approvals.glob("*.sig"))

    def test_answering_y_signs_and_the_gate_then_passes(self):
        request_id, action = self.publish_request()
        code, out, err, prompts = self.approve_with(request_id, "y")
        self.assertEqual(code, codes.OK, err)
        self.assertEqual(len(prompts), 1)
        self.assertIn("Signed approval for request " + request_id, out)
        verdict = gate.verify(self.state, action, self.cfg, self.env, consume=False)
        self.assertTrue(verdict.ok, verdict.reason)

    def signing_calls(self, result=None):
        """Record how ssh-keygen is started for signing. `result` stands in for the run."""
        calls = []
        real = gate.subprocess.run

        def spy(args, **kwargs):
            if "sign" in args:
                calls.append(kwargs)
                if result is not None:
                    return result
            return real(args, **kwargs)

        patcher = mock.patch.object(gate.subprocess, "run", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_the_passphrase_prompt_is_left_on_the_approvers_terminal(self):
        # ssh-keygen asks for the passphrase itself. A pipe on any of its three
        # streams can hide that prompt, so approve must hand it the terminal.
        request_id, action = self.publish_request()
        calls = self.signing_calls()
        code, _, err, _ = self.approve_with(request_id, "y")
        self.assertEqual(code, codes.OK, err)
        [kwargs] = calls
        for name in ("capture_output", "stdin", "stdout", "stderr", "input"):
            self.assertNotIn(name, kwargs, "ssh-keygen must inherit the terminal")
        self.assertTrue(gate.verify(self.state, action, self.cfg, self.env, consume=False).ok)

    def test_a_failed_signature_is_reported_from_the_exit_code(self):
        request_id, action = self.publish_request()
        failed = helpers.subprocess.CompletedProcess(args=[], returncode=7)   # no stderr to read
        self.signing_calls(result=failed)
        code, out, err, _ = self.approve_with(request_id, "y")
        self.assertEqual(code, codes.APPROVAL_INVALID)
        self.assertIn("signing failed", err)
        self.assertIn("exit 7", err)
        self.assertNotIn("Signed approval", out)
        self.assertEqual(list(self.state.approvals.glob("*.approval.json*")), [], "no unsigned approval is left behind")
        self.assertEqual(gate.verify(self.state, action, self.cfg, self.env, consume=False).code, "missing")

    def test_ssh_keygen_that_cannot_start_is_reported_not_raised(self):
        request_id, _ = self.publish_request()
        real = gate.subprocess.run

        def refuse(args, **kwargs):
            if "sign" in args:
                raise OSError("cannot start")
            return real(args, **kwargs)

        with mock.patch.object(gate.subprocess, "run", refuse):
            code, _, err, _ = self.approve_with(request_id, "y")
        self.assertEqual(code, codes.APPROVAL_INVALID)
        self.assertIn("cannot start", err)
        self.assertEqual(list(self.state.approvals.glob("*.approval.json*")), [])

    def test_any_other_answer_signs_nothing(self):
        request_id, action = self.publish_request()
        for answer in ("n", "", "yes please", "no"):
            with self.subTest(answer=answer):
                code, out, err, _ = self.approve_with(request_id, answer)
                self.assertEqual(code, codes.APPROVE_REFUSED, err)
                self.assertIn("Not signed.", out)
                self.assertEqual(self.signatures(), [])
                verdict = gate.verify(self.state, action, self.cfg, self.env, consume=False)
                self.assertEqual(verdict.code, "missing")

    def test_the_display_shows_every_field_of_the_action(self):
        request_id, action = self.publish_request()
        _, out, _, _ = self.approve_with(request_id, "n")
        for key, value in action.items():
            self.assertIn(f"{key}: {json.dumps(value)}", out)
        self.assertIn('"npm publish"', out)
        self.assertIn(json.dumps(action["cwd"]), out)

    def test_a_commit_request_shows_the_staged_diff_stat(self):
        (self.repo / "staged_file.txt").write_text("staged\n", encoding="utf-8")
        helpers.git(self.repo, "add", "staged_file.txt")
        self.assertNotEqual(self.git("commit", "-q", "-m", "change").returncode, 0)
        [request] = self.requests("git-commit")
        _, out, _, _ = self.approve_with(request["id"], "n")
        self.assertIn("staged_file.txt", out)
        self.assertIn("1 file changed", out)

    def test_no_key_given_and_none_configured_is_a_usage_error(self):
        request_id, _ = self.publish_request()
        self.assertIsNone(self.cfg["approver"].get("signing_key"))
        code, _, err, prompts = self.approve_with(request_id, "y", key=None)
        self.assertEqual(code, codes.USAGE)
        self.assertIn("no signing key", err)
        self.assertEqual(prompts, [])
        self.assertEqual(self.signatures(), [])

    def test_an_unknown_request_id_is_invalid(self):
        code, _, err, prompts = self.approve_with("0" * 16, "y")
        self.assertEqual(code, codes.APPROVAL_INVALID)
        self.assertIn("no pending request", err)
        self.assertEqual(prompts, [])
        self.assertEqual(self.signatures(), [])


if __name__ == "__main__":
    unittest.main()
