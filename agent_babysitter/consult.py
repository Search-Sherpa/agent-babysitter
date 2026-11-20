"""Lane 2: one consult with an advisor CLI from a different model family.

The builder's family is established first and fails closed. The advisor comes
from the seat routing table and can never be the builder's own family. Every
attempt is logged; a verdict also leaves a receipt, which is what counts as
"a consult on record". The budget is checked before anything runs.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import codes
from .config import FAMILIES

ADVISORS_DIR = Path(__file__).resolve().parent / "advisors"
WRAPPERS = {"claude": "ask-claude.sh", "gemini": "ask-gemini.sh", "codex": "ask-codex.sh", "grok": "ask-grok.sh"}
VERDICT_RE = re.compile(r"^\W{0,4}verdict\b[*_\s]{0,3}[:\-][*_\s]{0,4}\S", re.MULTILINE | re.IGNORECASE)
REPLY_FORMAT = 'Reply format: begin with one line "VERDICT: <your decision>", then at most 5 short bullets.'
TIMED_OUT = 124


class ConsultError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


@dataclass
class ConsultResult:
    code: int
    status: str  # "verdict" | "no_verdict" | "failed"
    advisor: str
    verdict: str
    output: str
    stderr: str = ""


def find_bash(cfg, env):
    """A POSIX bash for the wrappers. Never the WSL launcher in system32."""
    explicit = env.get("BABYSITTER_BASH") or cfg.get("bash")
    if explicit:
        return str(explicit)
    path = env.get("PATH")
    if os.name == "nt":
        git = shutil.which("git", path=path)
        if git:
            parents = Path(git).resolve().parents
            for level in (1, 2):
                if len(parents) > level:
                    candidate = parents[level] / "bin" / "bash.exe"
                    if candidate.is_file():
                        return str(candidate)
    found = shutil.which("bash", path=path)
    if found and "system32" not in found.lower():
        return found
    raise ConsultError("no POSIX bash found; set BABYSITTER_BASH or the config key 'bash'", codes.USAGE)


def host_family(cfg, env):
    """The builder's model family, or ConsultError(FAMILY_UNKNOWN)."""
    declared = env.get("BABYSITTER_HOST_FAMILY") or cfg.get("host_family")
    markers = {family for name, family in cfg["family_markers"].items() if env.get(name)}
    if declared and declared not in FAMILIES:
        raise ConsultError(f"builder family {declared!r} is not one of {', '.join(FAMILIES)}",
                           codes.FAMILY_UNKNOWN)
    if len(markers) > 1:
        raise ConsultError(f"session markers name more than one family ({', '.join(sorted(markers))})",
                           codes.FAMILY_UNKNOWN)
    if declared and markers and declared not in markers:
        raise ConsultError(f"declared builder family {declared!r} contradicts the session marker for "
                           f"{next(iter(markers))!r}", codes.FAMILY_UNKNOWN)
    if declared:
        return declared
    if markers:
        return next(iter(markers))
    raise ConsultError("builder family unknown; set BABYSITTER_HOST_FAMILY", codes.FAMILY_UNKNOWN)


def route(cfg, builder, seat):
    """(advisor, model, timeout) for this builder and seat."""
    try:
        entry = cfg["seats"][builder][seat]
    except KeyError:
        raise ConsultError(f"no advisor routed for builder {builder!r}, seat {seat!r}", codes.USAGE) from None
    advisor = entry["advisor"]
    base = cfg["advisors"].get(advisor, {})
    model = entry.get("model", base.get("model"))
    timeout = entry.get("timeout", base.get("timeout"))
    return advisor, model, timeout


def window(state):
    """Consult records since the most recent budget reset."""
    consults = state.records("consults")
    resets = state.records("budget-resets")
    start = resets[-1].get("consults_before", 0) if resets else 0
    return consults[start:]


def _receipt_path(state, decision):
    return state.receipts / (hashlib.sha256(decision.encode("utf-8")).hexdigest()[:32] + ".json")


def has_cover(state, cfg, decision):
    """(covered, reason): a verdict is on record, or the consult budget for it failed out."""
    if _receipt_path(state, decision).is_file():
        return True, "a consult verdict is on record"
    failed = [r for r in window(state) if r.get("decision") == decision and r.get("status") != "verdict"]
    if len(failed) >= cfg["budget"]["per_decision"]:
        return True, f"{len(failed)} consult attempts failed; the builder decides and that is logged"
    return False, "no consult on record for this decision"


def _verdict_line(output):
    match = VERDICT_RE.search(output)
    if not match:
        return ""
    return output[match.start():].lstrip().splitlines()[0].strip()


def _text(value):
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def consult(state, cfg, env, decision, seat, brief, cwd, advisor=None):
    raw_depth = env.get("ADVISOR_DEPTH", "0") or "0"
    # Anything but plain digits is treated as nested: an unreadable depth is not depth zero.
    depth = int(raw_depth) if raw_depth.isascii() and raw_depth.isdigit() else 1
    if depth >= 1:
        raise ConsultError("consult called from inside an advisor; consults do not nest", codes.RECURSION)

    builder = host_family(cfg, env)

    if advisor is None:
        advisor, model, timeout = route(cfg, builder, seat)
    else:
        base = cfg["advisors"].get(advisor, {})
        model, timeout = base.get("model"), base.get("timeout")
    if advisor == builder:
        raise ConsultError(f"advisor {advisor!r} is the builder's own family", codes.SELF_FAMILY)
    if advisor not in WRAPPERS:
        raise ConsultError(f"unknown advisor {advisor!r}; expected one of {', '.join(WRAPPERS)}", codes.USAGE)

    budget = cfg["budget"]
    recent = window(state)
    if len(recent) >= budget["per_window"]:
        raise ConsultError(f"consult budget exhausted: the window cap of {budget['per_window']} consults is "
                           "used up; the approver must reset the budget", codes.BUDGET_EXHAUSTED)
    attempts = sum(1 for r in recent if r.get("decision") == decision)
    if attempts >= budget["per_decision"]:
        raise ConsultError(f"consult budget exhausted: decision {decision!r} already had {attempts} attempts "
                           f"(per-decision cap {budget['per_decision']}); the approver must reset the budget",
                           codes.BUDGET_EXHAUSTED)

    bash = find_bash(cfg, env)
    timeout = int(timeout or 600)
    # A directory of its own: a wrapper may let the advisor read the brief's directory,
    # and that must not be the whole system temp directory.
    brief_dir = tempfile.mkdtemp(prefix="babysitter-brief-")
    prompt_path = os.path.join(brief_dir, "brief.md")
    try:
        with open(prompt_path, "w", encoding="utf-8", newline="\n") as prompt:
            prompt.write(brief.rstrip("\n") + "\n\n" + REPLY_FORMAT + "\n")
        args = [bash, (ADVISORS_DIR / WRAPPERS[advisor]).as_posix(), "--prompt-file",
                Path(prompt_path).as_posix(), Path(cwd).as_posix()]
        if advisor == "gemini" and model:
            args.append(model)
        run_env = dict(env)
        run_env["BABYSITTER_HOST_FAMILY"] = builder
        run_env[f"ASK_{advisor.upper()}_TIMEOUT"] = str(timeout)
        if model:
            run_env[f"ASK_{advisor.upper()}_MODEL"] = str(model)
        try:
            done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  env=run_env, timeout=timeout + 60)
            exit_code, output, stderr = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code, output, stderr = TIMED_OUT, _text(exc.stdout), _text(exc.stderr)
    finally:
        shutil.rmtree(brief_dir, ignore_errors=True)

    verdict = _verdict_line(output) if exit_code == 0 else ""
    if exit_code != 0:
        status = "failed"
    elif not verdict:
        status = "no_verdict"
    else:
        status = "verdict"

    state.append("consults", {"decision": decision, "seat": seat, "builder": builder, "advisor": advisor,
                              "model": model, "status": status, "exit": exit_code})
    if status == "verdict":
        receipt = {"decision": decision, "seat": seat, "builder": builder, "advisor": advisor, "model": model,
                   "verdict": verdict, "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest()}
        # open() rather than Path.write_text: its newline argument needs Python 3.10.
        with open(_receipt_path(state, decision), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return ConsultResult(codes.OK if status == "verdict" else codes.CONSULT_FAILED,
                         status, advisor, verdict, output, stderr)
