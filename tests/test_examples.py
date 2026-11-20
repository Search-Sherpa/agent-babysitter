"""The shipped example files must be usable as they stand."""

import json
import tempfile
import unittest

from agentsitter import config, consult, hooks as hooks_mod, lanes

from .helpers import REPO_ROOT


class ExampleConfigTest(unittest.TestCase):
    def load(self):
        text = (REPO_ROOT / "agentsitter.config.example.json").read_text(encoding="utf-8")
        return config.validate(config.merge(config.defaults(), json.loads(text)))

    def test_the_example_config_passes_validation(self):
        cfg = self.load()
        self.assertEqual(cfg["approver"]["identity"], "approver")

    def test_the_example_seat_override_routes_to_a_gemini_model(self):
        advisor, model, timeout = consult.route(self.load(), "claude", "architecture")
        self.assertEqual(advisor, "gemini")
        self.assertTrue(model.startswith("gemini-"), model)
        # The other seats keep the defaults the override did not touch.
        self.assertEqual(consult.route(self.load(), "claude", "code")[0], "codex")

    def test_the_example_extra_rule_is_lane_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(lanes.classify("make release", tmp, self.load()).lane, 3)


class ExampleHookSettingsTest(unittest.TestCase):
    def test_the_hook_settings_run_the_agent_hook_on_tool_use_and_stop(self):
        text = (REPO_ROOT / "examples" / "agent-hook-settings.json").read_text(encoding="utf-8")
        hooks = json.loads(text)["hooks"]
        [pre] = hooks["PreToolUse"]
        self.assertEqual(set(pre["matcher"].split("|")), hooks_mod.SHELL_TOOLS | hooks_mod.EDIT_TOOLS)
        self.assertEqual(pre["hooks"][0]["command"], "agentsitter hook agent")
        [stop] = hooks["Stop"]
        self.assertEqual(stop["hooks"][0]["command"], "agentsitter hook agent")


if __name__ == "__main__":
    unittest.main()
