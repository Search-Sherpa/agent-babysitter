"""Lane 2 mechanics: cross-family routing, self-exclusion, verdict checks and the consult budget."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_babysitter import codes, config, consult

from . import helpers

WRAPPERS = {"claude": "ask-claude.sh", "gemini": "ask-gemini.sh", "codex": "ask-codex.sh", "grok": "ask-grok.sh"}
STUB_NAME = {"claude": "claude", "gemini": "agy", "codex": "codex", "grok": "grok"}
TEST_CONFIG = {"advisors": {"gemini": {"model": "gemini-test-model"}}}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.brief = self.tmp / "brief.md"
        self.brief.write_text("Question: option A or option B?\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, family="claude", stub_mode="verdict", config_override=None, **extra):
        env = helpers.base_env(self.tmp, config=config.merge(TEST_CONFIG, config_override or {}),
                               family=family, stub_mode=stub_mode)
        env.update(extra)
        return env

    def stub_calls(self):
        log = self.tmp / "stub-calls.log"
        return log.read_text(encoding="utf-8").split() if log.is_file() else []


class WrapperTest(Base):
    """The shell wrappers hold their guards even when called directly."""

    def run_wrapper(self, family, env):
        bash = consult.find_bash(config.defaults(), env)
        script = consult.ADVISORS_DIR / WRAPPERS[family]
        args = [bash, script.as_posix(), "--prompt-file", self.brief.as_posix(), self.work.as_posix()]
        if family == "gemini":
            args.append("gemini-test-model")
        return subprocess.run(args, capture_output=True, text=True, env=env)

    def builder_for(self, advisor):
        return "codex" if advisor == "claude" else "claude"

    def test_each_wrapper_returns_the_advisor_answer(self):
        for family in WRAPPERS:
            with self.subTest(advisor=family):
                done = self.run_wrapper(family, self.env(family=self.builder_for(family)))
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("VERDICT: Option A", done.stdout)
        self.assertEqual(sorted(self.stub_calls()), sorted(STUB_NAME.values()))

    def test_each_wrapper_refuses_its_own_family(self):
        for family in WRAPPERS:
            with self.subTest(advisor=family):
                done = self.run_wrapper(family, self.env(family=family))
                self.assertEqual(done.returncode, 7, done.stderr)
        self.assertEqual(self.stub_calls(), [])

    def test_each_wrapper_fails_closed_when_the_builder_is_unknown(self):
        for family in WRAPPERS:
            with self.subTest(advisor=family):
                done = self.run_wrapper(family, self.env(family=None))
                self.assertEqual(done.returncode, 6, done.stderr)
        self.assertEqual(self.stub_calls(), [])

    def test_each_wrapper_blocks_a_call_from_inside_an_advisor(self):
        for family in WRAPPERS:
            with self.subTest(advisor=family):
                done = self.run_wrapper(family, self.env(family=self.builder_for(family), ADVISOR_DEPTH="1"))
                self.assertEqual(done.returncode, 4, done.stderr)
        self.assertEqual(self.stub_calls(), [])

    def test_a_blank_answer_is_a_failure_not_a_pass(self):
        for family in WRAPPERS:
            with self.subTest(advisor=family):
                done = self.run_wrapper(family, self.env(family=self.builder_for(family), stub_mode="empty"))
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual(done.stdout.strip(), "")

    def test_each_wrapper_rejects_a_depth_that_is_not_a_plain_number(self):
        # Bash evaluates an arithmetic value as an expression, so a depth that is
        # not digits could run a command before any guard. -1 would also slip
        # under the nesting check.
        marker = self.tmp / "ran-from-depth"
        payloads = ("BASH_VERSINFO[$(touch '%s')0]" % marker.as_posix(), "-1", "1 + 1", "x")
        for family in WRAPPERS:
            for depth in payloads:
                with self.subTest(advisor=family, depth=depth):
                    done = self.run_wrapper(family, self.env(family=self.builder_for(family), ADVISOR_DEPTH=depth))
                    self.assertEqual(done.returncode, 2, done.stderr)
                    self.assertFalse(marker.exists(), "the depth value was run as a command")
        self.assertEqual(self.stub_calls(), [])

    def test_each_wrapper_treats_any_depth_but_zero_as_nested(self):
        # A number too large for the shell makes a numeric comparison fail, and a
        # failed comparison must not read as "depth zero".
        for family in WRAPPERS:
            for depth in ("18446744073709551616", "00", "2"):
                with self.subTest(advisor=family, depth=depth):
                    done = self.run_wrapper(family, self.env(family=self.builder_for(family), ADVISOR_DEPTH=depth))
                    self.assertEqual(done.returncode, 4, done.stderr)
        self.assertEqual(self.stub_calls(), [])

    def test_a_prompt_file_named_like_an_option_is_read_as_a_file(self):
        # `cat --help` prints cat's help and `cat -` reads stdin. Either would hand
        # the advisor something other than the brief. (The codex wrapper copies the
        # file and feeds it on stdin, so it has no such read.)
        bash = consult.find_bash(config.defaults(), self.env())
        for name in ("--help", "-"):
            (self.work / name).write_text("Question: plum or quince?\n", encoding="utf-8")
            for family in ("claude", "gemini", "grok"):
                with self.subTest(advisor=family, name=name):
                    seen = self.tmp / f"args-{family}-{len(name)}.log"
                    args = [bash, (consult.ADVISORS_DIR / WRAPPERS[family]).as_posix(), "--prompt-file", name,
                            self.work.as_posix()]
                    if family == "gemini":
                        args.append("gemini-test-model")
                    done = subprocess.run(args, capture_output=True, text=True, cwd=str(self.work),
                                          stdin=subprocess.DEVNULL,
                                          env=self.env(family=self.builder_for(family), STUB_ARGS_LOG=str(seen)))
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertIn("plum or quince", seen.read_text(encoding="utf-8"))

    def test_with_no_way_to_enforce_a_timeout_the_advisor_is_not_started(self):
        bash = consult.find_bash(config.defaults(), self.env())
        runner = (consult.ADVISORS_DIR / "_runner.sh").as_posix()
        script = f'. "{runner}"; PATH=/nonexistent; _rr_run_with_timeout 1 echo started; echo "rc=$?"'
        done = subprocess.run([bash, "-c", script], capture_output=True, text=True, env=self.env())
        self.assertNotIn("started", done.stdout)
        self.assertIn("rc=2", done.stdout, done.stderr)


class ConsultCommandTest(Base):
    def consult(self, env, decision="d1", seat="code", *more):
        return helpers.cli(["consult", "--decision", decision, "--seat", seat,
                            "--brief-file", str(self.brief), *more], env, self.work)

    def receipts(self):
        return list((self.tmp / "state").glob("*/receipts/*.json"))

    def test_a_verdict_is_returned_and_put_on_record(self):
        done = self.consult(self.env())
        self.assertEqual(done.returncode, codes.OK, done.stderr)
        self.assertIn("VERDICT: Option A", done.stdout)
        self.assertEqual(self.stub_calls(), ["codex"])
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual((receipt["decision"], receipt["advisor"], receipt["builder"]), ("d1", "codex", "claude"))

    def test_routing_follows_the_builder_family(self):
        expected = {"claude": "codex", "gemini": "codex", "codex": "grok", "grok": "codex"}
        for builder, advisor in expected.items():
            with self.subTest(builder=builder):
                done = self.consult(self.env(family=builder), f"d-{builder}")
                self.assertEqual(done.returncode, codes.OK, done.stderr)
                self.assertEqual(self.stub_calls()[-1], STUB_NAME[advisor])

    def test_asking_for_an_advisor_of_the_builders_own_family_is_blocked(self):
        done = self.consult(self.env(), "d1", "code", "--advisor", "claude")
        self.assertEqual(done.returncode, codes.SELF_FAMILY, done.stderr)
        self.assertEqual(self.stub_calls(), [])

    def test_unknown_builder_family_fails_closed(self):
        done = self.consult(self.env(family=None))
        self.assertEqual(done.returncode, codes.FAMILY_UNKNOWN, done.stderr)
        self.assertEqual(self.stub_calls(), [])

    def test_a_declared_family_that_contradicts_the_session_fails_closed(self):
        done = self.consult(self.env(family="grok", CLAUDECODE="1"))
        self.assertEqual(done.returncode, codes.FAMILY_UNKNOWN, done.stderr)
        self.assertEqual(self.stub_calls(), [])

    def test_a_session_marker_alone_identifies_the_builder(self):
        done = self.consult(self.env(family=None, CLAUDECODE="1"))
        self.assertEqual(done.returncode, codes.OK, done.stderr)
        self.assertEqual(self.stub_calls(), ["codex"])

    def test_narration_without_a_verdict_is_not_a_review(self):
        done = self.consult(self.env(stub_mode="narration"))
        self.assertEqual(done.returncode, codes.CONSULT_FAILED, done.stderr)
        self.assertEqual(self.receipts(), [])

    def test_an_advisor_error_is_not_a_review(self):
        done = self.consult(self.env(stub_mode="fail"))
        self.assertEqual(done.returncode, codes.CONSULT_FAILED, done.stderr)
        self.assertEqual(self.receipts(), [])

    def test_a_third_call_on_one_decision_is_refused(self):
        env = self.env(stub_mode="narration")
        self.assertEqual(self.consult(env).returncode, codes.CONSULT_FAILED)
        self.assertEqual(self.consult(env).returncode, codes.CONSULT_FAILED)
        third = self.consult(self.env())
        self.assertEqual(third.returncode, codes.BUDGET_EXHAUSTED, third.stderr)
        self.assertEqual(len(self.stub_calls()), 2)

    def test_the_window_budget_stops_further_consults(self):
        env = self.env(config_override={"budget": {"per_window": 3}})
        for n in range(3):
            self.assertEqual(self.consult(env, f"d{n}").returncode, codes.OK)
        fourth = self.consult(env, "d3")
        self.assertEqual(fourth.returncode, codes.BUDGET_EXHAUSTED, fourth.stderr)
        self.assertEqual(len(self.stub_calls()), 3)

    def test_a_non_ascii_answer_reaches_stdout_as_utf8(self):
        env = self.env(stub_mode="unicode")
        # No interpreter-wide override: the CLI itself must choose UTF-8.
        for name in ("PYTHONIOENCODING", "PYTHONUTF8"):
            env.pop(name, None)
        done = helpers.cli_bytes(["consult", "--decision", "d1", "--seat", "code",
                                  "--brief-file", str(self.brief)], env, self.work)
        self.assertEqual(done.returncode, codes.OK, done.stderr)
        check_mark = chr(0x2713)  # the stub prints it as UTF-8 bytes 342 234 223 (octal)
        self.assertIn(check_mark.encode("utf-8"), done.stdout)
        self.assertIn(check_mark.encode("utf-8"), done.stderr)
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertIn(check_mark, receipt["verdict"])

    def test_a_consult_from_inside_an_advisor_is_blocked(self):
        done = self.consult(self.env(ADVISOR_DEPTH="1"))
        self.assertEqual(done.returncode, codes.RECURSION, done.stderr)
        self.assertEqual(self.stub_calls(), [])

    def test_a_depth_that_cannot_be_read_counts_as_nested(self):
        for depth in ("bogus", "-1", "1 + 1"):
            with self.subTest(depth=depth):
                done = self.consult(self.env(ADVISOR_DEPTH=depth))
                self.assertEqual(done.returncode, codes.RECURSION, done.stderr)
        self.assertEqual(self.stub_calls(), [])
        self.assertEqual(self.receipts(), [])

    def test_the_brief_is_handed_over_in_a_directory_of_its_own(self):
        # The Gemini wrapper lets the advisor read the brief's directory when the
        # brief is long. That directory must hold the brief and nothing else.
        seen = []
        real_run = consult.subprocess.run

        def spy(args, **kwargs):
            if "--prompt-file" in args:
                brief = Path(args[args.index("--prompt-file") + 1])
                seen.append((brief.parent, sorted(p.name for p in brief.parent.iterdir())))
            return real_run(args, **kwargs)

        consult.subprocess.run = spy
        try:
            env = self.env()
            st = consult_state(self.work, env)
            result = consult.consult(st, config.merge(config.defaults(), TEST_CONFIG), env, "d1", "code",
                                     "Question?\n", self.work)
        finally:
            consult.subprocess.run = real_run
        self.assertEqual(result.status, "verdict", result.stderr)
        [(directory, names)] = seen
        self.assertEqual(len(names), 1)
        self.assertNotEqual(directory, Path(tempfile.gettempdir()))
        self.assertFalse(directory.exists(), "the brief directory was left behind")


def consult_state(work, env):
    from agent_babysitter import lanes, state
    return state.for_project(lanes.project_root(work), env)


if __name__ == "__main__":
    unittest.main()
