"""The hard gate: signed, single-use, action-bound human approvals.

An approval is a small JSON document naming one exact action, signed with the
approver's SSH key (`ssh-keygen -Y sign`). The gate holds only the public key.
A file that merely exists proves nothing: the gate checks the signature, that
the document names this action, that it has not expired, and that it has not
been used before.

What this does not do: if the private key sits unencrypted where the governed
agent can read it, the agent can sign for itself. Use a passphrase-protected or
hardware-backed key.
"""

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod


class GateError(Exception):
    """The gate cannot do what was asked. Callers treat this as 'not approved'."""


@dataclass(frozen=True)
class Verdict:
    ok: bool
    code: str  # approved | missing | spent | invalid
    reason: str
    request_id: str


def canonical(obj):
    """One byte representation per document, so a signature covers one meaning."""
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def action_digest(action):
    return "sha256:" + hashlib.sha256(canonical(action)).hexdigest()


def request_id_for(action):
    return action_digest(action).split(":", 1)[1][:16]


def _paths(state, request_id):
    base = state.approvals / request_id
    approval = Path(f"{base}.approval.json")
    return Path(f"{base}.request.json"), approval, Path(f"{approval}.sig")


def _ssh_keygen():
    tool = shutil.which("ssh-keygen")
    if not tool:
        raise GateError("ssh-keygen was not found on PATH; it is required to sign and verify approvals")
    return tool


def detail_digest(detail):
    return hashlib.sha256(canonical(detail)).hexdigest()


def open_request(state, action, detail=None):
    """Record that an action is waiting for approval. Returns the request id.

    `detail` is what the action's own hash stands for (the full input of an edit),
    kept beside the request so the approver is shown the thing itself.
    """
    request_id = request_id_for(action)
    request, _, _ = _paths(state, request_id)
    document = {"v": 1, "action": action, "digest": action_digest(action), "requested_at": int(time.time())}
    if detail is not None:
        document["detail"] = detail
    request.write_bytes(canonical(document))
    return request_id


def load_request(state, request_id):
    request, _, _ = _paths(state, request_id)
    if not request.is_file():
        raise GateError(f"no pending request {request_id}")
    try:
        document = json.loads(request.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise GateError(f"request {request_id} is not valid JSON") from exc
    if not isinstance(document, dict) or "action" not in document:
        raise GateError(f"request {request_id} is malformed")
    # The digest is recomputed from the action shown to the approver, never trusted.
    if document.get("digest") != action_digest(document["action"]) or request_id_for(document["action"]) != request_id:
        raise GateError(f"request {request_id} does not match the action it displays; refusing to sign")
    action = document["action"]
    if isinstance(action, dict) and "input_sha256" in action:
        # The action names its content only by hash. The content must be here, and be that content.
        try:
            matches = "detail" in document and detail_digest(document["detail"]) == action["input_sha256"]
        except (TypeError, ValueError):
            matches = False
        if not matches:
            raise GateError(f"request {request_id} does not carry the edit it describes; refusing to sign")
    return document


def sign(state, request_id, key_path, cfg, now=None, interactive=False):
    """Approver side: sign one pending request. Returns the approval document.

    `interactive` is for a person at a terminal: ssh-keygen then inherits all
    three streams, because it asks for the key's passphrase itself and a pipe
    on any of them can hide that prompt. Its own messages go straight to the
    terminal, and failure is read from the exit code and the missing signature.
    """
    request = load_request(state, request_id)
    key_path = Path(key_path).expanduser()
    if not key_path.is_file():
        raise GateError(f"signing key not found: {key_path}")
    now = int(time.time()) if now is None else int(now)
    document = {
        "v": 1,
        "action": request["action"],
        "digest": request["digest"],
        "nonce": secrets.token_hex(16),
        "approved_at": now,
        "expires_at": now + cfg["approval"]["ttl_seconds"],
        "approver": cfg["approver"]["identity"],
    }
    _, approval, signature = _paths(state, request_id)
    signature.unlink(missing_ok=True)
    approval.write_bytes(canonical(document))
    command = [_ssh_keygen(), "-Y", "sign", "-f", str(key_path), "-n", cfg["approval"]["namespace"], str(approval)]
    try:
        done = subprocess.run(command) if interactive else subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:
        approval.unlink(missing_ok=True)
        raise GateError(f"signing failed: ssh-keygen could not run: {exc}")
    if done.returncode != 0 or not signature.is_file():
        approval.unlink(missing_ok=True)
        signature.unlink(missing_ok=True)
        said = (getattr(done, "stderr", None) or "").strip()
        if not said:
            said = (f"ssh-keygen ended with exit {done.returncode}" if done.returncode != 0
                    else "ssh-keygen produced no signature")
        raise GateError(f"signing failed: {said}")
    return document


def _signature_ok(payload, signature, cfg, env):
    anchor = config_mod.allowed_signers_path(cfg, env)
    if not anchor.is_file():
        return False, f"no trust anchor: {anchor} does not exist"
    try:
        done = subprocess.run(
            [_ssh_keygen(), "-Y", "verify", "-f", str(anchor), "-I", cfg["approver"]["identity"],
             "-n", cfg["approval"]["namespace"], "-s", str(signature)],
            input=payload, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError, GateError) as exc:
        return False, f"signature check could not run: {exc}"
    if done.returncode != 0:
        return False, "signature does not verify against the trusted approver key"
    return True, ""


def verify(state, action, cfg, env=None, now=None, consume=True):
    """Gate side: is there a valid, unused, unexpired signed approval for this exact action?"""
    digest = action_digest(action)
    request_id = request_id_for(action)
    _, approval, signature = _paths(state, request_id)

    def no(code, reason):
        return Verdict(False, code, reason, request_id)

    if not approval.is_file() or not signature.is_file():
        return no("missing", "no signed approval on file")
    try:
        payload = approval.read_bytes()
        document = json.loads(payload.decode("utf-8"))
        approved_at, expires_at = int(document["approved_at"]), int(document["expires_at"])
        claimed, shown = document["digest"], document["action"]
        shown_digest = action_digest(shown)
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
        return no("invalid", "approval document is malformed")
    if claimed != digest or shown_digest != digest:
        return no("invalid", "approval was issued for a different action")

    def clock():
        return int(time.time()) if now is None else int(now)

    if expires_at - approved_at > cfg["approval"]["ttl_seconds"]:
        return no("invalid", "approval lifetime exceeds the configured limit")
    if clock() > expires_at:
        return no("invalid", "approval has expired")
    marker = state.spent / hashlib.sha256(payload).hexdigest()
    if marker.exists():
        return no("spent", "approval was already used; a new one is required")
    good, why = _signature_ok(payload, signature, cfg, env)
    if not good:
        return no("invalid", why)
    if clock() > expires_at:  # the signature check takes time; the clock is read again after it
        return no("invalid", "approval has expired")
    if consume:
        try:
            fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            return no("spent", "approval was already used; a new one is required")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{clock()} {request_id}\n")
    return Verdict(True, "approved", "valid signed approval", request_id)


def pending(state):
    """Requests on file, newest first, each tagged with whether a usable approval exists."""
    out = []
    for path in sorted(state.approvals.glob("*.request.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        request_id = path.name.split(".", 1)[0]
        try:
            out.append((request_id, load_request(state, request_id)))
        except GateError:
            continue
    return out
