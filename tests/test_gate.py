"""Hard gate: an approval counts only if it carries a valid signature over this exact action."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from agentsitter import config, gate
from agentsitter.state import State

from . import helpers

ACTION = {"kind": "command", "command": "npm publish", "cwd": "/work/project"}
OTHER = {"kind": "command", "command": "git push --force", "cwd": "/work/project"}


class GateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.key, public = helpers.make_key(tmp, "approver_key")
        self.cfg = config.merge(config.defaults(), {
            "approver": {"allowed_signers": str(helpers.trust(tmp, public))},
        })
        self.state = State(tmp / "state")
        self.tmp = tmp

    def tearDown(self):
        self._tmp.cleanup()

    def approve(self, action, key=None, now=None):
        request_id = gate.open_request(self.state, action)
        gate.sign(self.state, request_id, key or self.key, self.cfg, now=now)
        return request_id

    def test_unapproved_action_is_missing(self):
        verdict = gate.verify(self.state, ACTION, self.cfg)
        self.assertEqual((verdict.ok, verdict.code), (False, "missing"))

    def test_signed_approval_verifies(self):
        self.approve(ACTION)
        verdict = gate.verify(self.state, ACTION, self.cfg)
        self.assertEqual((verdict.ok, verdict.code), (True, "approved"))

    def test_a_file_that_merely_exists_does_not_pass(self):
        request_id = gate.open_request(self.state, ACTION)
        now = int(time.time())
        document = {"v": 1, "action": ACTION, "digest": gate.action_digest(ACTION),
                    "nonce": "00" * 16, "approved_at": now, "expires_at": now + 60,
                    "approver": "approver"}
        approval = self.state.approvals / f"{request_id}.approval.json"
        approval.write_bytes(gate.canonical(document))
        Path(str(approval) + ".sig").write_text("approved\n", encoding="utf-8")
        verdict = gate.verify(self.state, ACTION, self.cfg)
        self.assertEqual((verdict.ok, verdict.code), (False, "invalid"))

    def test_signature_from_an_untrusted_key_is_rejected(self):
        stranger, _ = helpers.make_key(self.tmp, "stranger_key")
        self.approve(ACTION, key=stranger)
        verdict = gate.verify(self.state, ACTION, self.cfg)
        self.assertEqual((verdict.ok, verdict.code), (False, "invalid"))

    def test_approval_for_another_action_is_rejected(self):
        signed_id = self.approve(ACTION)
        other_id = gate.open_request(self.state, OTHER)
        for suffix in (".approval.json", ".approval.json.sig"):
            source = self.state.approvals / f"{signed_id}{suffix}"
            (self.state.approvals / f"{other_id}{suffix}").write_bytes(source.read_bytes())
        verdict = gate.verify(self.state, OTHER, self.cfg)
        self.assertEqual((verdict.ok, verdict.code), (False, "invalid"))

    def test_tampered_approval_is_rejected(self):
        request_id = self.approve(ACTION)
        approval = self.state.approvals / f"{request_id}.approval.json"
        document = json.loads(approval.read_text(encoding="utf-8"))
        document["nonce"] = "ff" * 16
        approval.write_bytes(gate.canonical(document))
        verdict = gate.verify(self.state, ACTION, self.cfg)
        self.assertEqual((verdict.ok, verdict.code), (False, "invalid"))

    def test_approval_is_single_use(self):
        self.approve(ACTION)
        self.assertTrue(gate.verify(self.state, ACTION, self.cfg).ok)
        again = gate.verify(self.state, ACTION, self.cfg)
        self.assertEqual((again.ok, again.code), (False, "spent"))

    def test_a_fresh_approval_reopens_a_spent_action(self):
        self.approve(ACTION)
        self.assertTrue(gate.verify(self.state, ACTION, self.cfg).ok)
        self.approve(ACTION)
        self.assertTrue(gate.verify(self.state, ACTION, self.cfg).ok)

    def test_peeking_does_not_consume(self):
        self.approve(ACTION)
        self.assertTrue(gate.verify(self.state, ACTION, self.cfg, consume=False).ok)
        self.assertTrue(gate.verify(self.state, ACTION, self.cfg).ok)

    def test_expired_approval_is_rejected(self):
        signed_at = int(time.time()) - self.cfg["approval"]["ttl_seconds"] - 5
        self.approve(ACTION, now=signed_at)
        verdict = gate.verify(self.state, ACTION, self.cfg)
        self.assertEqual((verdict.ok, verdict.code), (False, "invalid"))
        self.assertIn("expired", verdict.reason)

    def test_an_approval_that_expires_while_its_signature_is_checked_is_rejected(self):
        self.approve(ACTION)
        real_check, real_time, clock = gate._signature_ok, gate.time.time, {"now": time.time()}

        def slow_check(*args):
            clock["now"] += self.cfg["approval"]["ttl_seconds"] + 5
            return real_check(*args)

        gate._signature_ok, gate.time.time = slow_check, lambda: clock["now"]
        try:
            verdict = gate.verify(self.state, ACTION, self.cfg)
        finally:
            gate._signature_ok, gate.time.time = real_check, real_time
        self.assertEqual((verdict.ok, verdict.code), (False, "invalid"))
        self.assertIn("expired", verdict.reason)
        self.assertEqual(list(self.state.spent.iterdir()), [], "a refused approval must not be spent")

    def test_a_malformed_approval_is_a_refusal_not_a_crash(self):
        request_id = gate.open_request(self.state, ACTION)
        approval = self.state.approvals / f"{request_id}.approval.json"
        Path(str(approval) + ".sig").write_text("x\n", encoding="utf-8")
        digest = gate.action_digest(ACTION)
        bodies = (
            b'{"approved_at": 1e999, "expires_at": 0, "digest": "%s", "action": {}}' % digest.encode("ascii"),
            b'{"approved_at": null, "expires_at": 0, "digest": "%s", "action": {}}' % digest.encode("ascii"),
            b"[1, 2, 3]", b'"text"', b"\xff\xfe", b"[" * 200000 + b"]" * 200000,
        )
        for body in bodies:
            with self.subTest(body=body):
                approval.write_bytes(body)
                verdict = gate.verify(self.state, ACTION, self.cfg)
                self.assertEqual((verdict.ok, verdict.code), (False, "invalid"))

    def test_missing_trust_anchor_fails_closed(self):
        self.approve(ACTION)
        cfg = config.merge(self.cfg, {"approver": {"allowed_signers": str(self.tmp / "absent")}})
        verdict = gate.verify(self.state, ACTION, cfg)
        self.assertEqual((verdict.ok, verdict.code), (False, "invalid"))

    def test_sign_refuses_a_request_whose_digest_was_swapped(self):
        request_id = gate.open_request(self.state, ACTION)
        request = self.state.approvals / f"{request_id}.request.json"
        forged = json.loads(request.read_text(encoding="utf-8"))
        forged["action"] = OTHER  # shows one action, carries the digest of another
        request.write_bytes(gate.canonical(forged))
        with self.assertRaises(gate.GateError):
            gate.sign(self.state, request_id, self.key, self.cfg)


if __name__ == "__main__":
    unittest.main()
