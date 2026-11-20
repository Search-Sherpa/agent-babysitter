"""Enforcement: turn a lane classification into a decision the agent must obey.

Lane 1 runs. Lane 2 runs once a cross-family consult is on record for this exact
decision. Lane 3 runs only on a valid signed approval for this exact action;
otherwise a request is opened for the approver.
"""

import hashlib
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import codes, consult, gate, hooks, lanes

RUN_PREFIX = re.compile(
    r"""^\s*(?:(?:agent-)?babysitter(?:\.exe)?|(?:"[^"]*python[^"]*"|'[^']*python[^']*'|\S*python\S*)\s+-m\s+agent_babysitter)\s+run\s+--(?:\s+|$)""",
    re.IGNORECASE,
)


@dataclass
class Outcome:
    code: int
    lane: int
    status: str
    message: str
    decision: str = ""
    seat: str = ""
    request_id: str = ""

    def as_dict(self):
        return {"lane": self.lane, "code": self.code, "status": self.status, "message": self.message,
                "decision": self.decision, "seat": self.seat, "request_id": self.request_id}


def _slash(path):
    return os.path.realpath(str(path)).replace("\\", "/")


def command_action(command, cwd):
    return {"kind": "command", "command": RUN_PREFIX.sub("", command.strip(), count=1).strip(),
            "cwd": _slash(cwd)}


def approval_outcome(state, action, cfg, env, consume, lane, what, detail=None):
    """Lane 3 for any action: approved, or a request opened with instructions."""
    verdict = gate.verify(state, action, cfg, env, consume=consume)
    if verdict.ok:
        return Outcome(codes.OK, lane, "approved", f"{what}: a valid signed approval is on file.",
                       request_id=verdict.request_id)
    request_id = gate.open_request(state, action, detail)
    if verdict.code == "invalid":
        code, status = codes.APPROVAL_INVALID, "approval-invalid"
        lead = f"{what} is lane 3 and the approval on file is not valid ({verdict.reason})."
    else:
        code, status = codes.APPROVAL_REQUIRED, "approval-required"
        lead = f"{what} is lane 3 and needs a signed human approval."
    message = (f"{lead} Request {request_id} is open. Ask the approver to run "
               f"`babysitter approve {request_id}` in their own terminal, then retry the identical "
               "action. An advisor's opinion never substitutes for this approval.")
    return Outcome(code, lane, status, message, request_id=request_id)


def consult_outcome(state, cfg, decision, seat, lane, what):
    """Lane 2 for any decision: covered, budget exhausted, or consult required."""
    covered, _ = consult.has_cover(state, cfg, decision)
    if covered:
        verdict_on_file = consult._receipt_path(state, decision).is_file()
        status = "consulted" if verdict_on_file else "consult-fallback"
        note = ("a consult verdict is on record" if verdict_on_file
                else "the consult attempts failed; proceed on your own judgment, which is logged")
        return Outcome(codes.OK, lane, status, f"{what}: {note}.", decision=decision, seat=seat)
    if len(consult.window(state)) >= cfg["budget"]["per_window"]:
        message = (f"{what} needs a consult, but the consult budget for this window is used up. Stop and "
                   "report your progress to the approver, who can run `babysitter budget reset`.")
        return Outcome(codes.BUDGET_EXHAUSTED, lane, "budget-exhausted", message, decision=decision, seat=seat)
    message = (f"{what} is lane 2 and needs one cross-family consult on record first. Write the question "
               "and its context to a brief file, then run: "
               f"babysitter consult --decision {decision} --seat {seat} --brief-file <file> "
               "and retry once you have weighed the verdict.")
    return Outcome(codes.CONSULT_REQUIRED, lane, "consult-required", message, decision=decision, seat=seat)


# Wrappers that only time or reshape output, taken with no options of their own.
OUTPUT_WRAPPERS = {"rtk", "time"}
# Words that set variables for what runs after them, or load a file that may.
ENV_WORDS = {"export", "unset", "declare", "typeset", "readonly", "local", "setx", "setenv", "source", "."}
POWERSHELL_ENV = re.compile(
    r"\$env:\w+\s*[-+]?=|SetEnvironmentVariable"
    r"|\b(?:Set-Item|si|New-Item|ni|Set-Content|sc|Remove-Item|ri|rm|del|Clear-Item|cli)\b[^;|\n]*\benv:",
    re.IGNORECASE)


def _sets_environment(command):
    """True when the command line may change the environment a later program runs in.

    Our git hook finds its config, trust anchor and state through HOME, its tools
    through PATH and git's own behaviour through GIT_*. A commit that sets any
    variable is therefore not left to the hook alone.
    """
    text = lanes.strip_heredocs(command)
    if POWERSHELL_ENV.search(text):
        return True
    for segment in lanes.split_segments(text):
        tokens = lanes._tokens(segment)
        while tokens:
            word = os.path.basename(tokens[0].replace("\\", "/")).lower()
            if lanes.ASSIGNMENT.match(tokens[0]) or word in ENV_WORDS:
                return True
            if word == "env" and len(tokens) > 1 and tokens[1].startswith("-"):
                return True
            if word == "set" and any(lanes.ASSIGNMENT.match(token) for token in tokens[1:]):
                return True
            if word not in lanes.PREFIX_WORDS:
                break
            tokens = tokens[1:]
            while tokens and (tokens[0].startswith("-") or tokens[0].isdigit()):
                tokens = tokens[1:]
    return False


def _changes_directory(command):
    segments = lanes.split_segments(lanes.strip_heredocs(command))
    return any(lanes._command_and_args(segment)[0] in lanes.CHDIR_WORDS for segment in segments)


def _git_words(segment):
    """The words of a segment that runs git itself, unquoted as the shell would, else None.

    A wrapper that only times or reshapes output may stand in front. Any other
    wrapper may change how git runs: `exec -c` clears the environment, and `env`,
    `command`, `nohup` and `xargs` take options we do not model.
    """
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:
        return None
    if len(words) > 1 and lanes._word(words[0]) in OUTPUT_WRAPPERS:
        words = words[1:]
    return words if words and lanes._word(words[0]) == "git" else None


def _git_hook_covers(match, command, cwd, chdir):
    """Our installed git hook will gate this match only if every part of the command
    line is git itself, git acts on cwd's repository, and it runs in the environment
    we were started in."""
    if chdir or _sets_environment(command):
        return False
    segments = lanes.split_segments(lanes.strip_heredocs(command))
    if any(_git_words(segment) is None for segment in segments):
        # Anything beside git on the line can change what `git` means or where it runs:
        # a shell function or alias named git, eval, a variable set in a way we do not model.
        return False
    words = _git_words(match.segment)
    if words is None:
        return False
    args = words[1:]
    if not args or args[0].startswith("-"):
        # An option before the subcommand (-C, -c, --git-dir, --work-tree, --config-env,
        # --namespace, ...) can move git to another repository or change which hooks run.
        return False
    options = args[1:args.index("--")] if "--" in args else args[1:]
    if match.rule_id == "git-rebase" and any(arg.startswith("-") for arg in options):
        # The pre-rebase hook is told only the upstream and the branch. --onto (which git
        # also accepts abbreviated) and every other option are outside what it can bind.
        return False
    if any(arg.startswith("--no-v") for arg in options):
        return False  # --no-verify, which git accepts abbreviated, skips the hook
    if match.rule_id == "git-commit":
        if any(arg.startswith("--am") for arg in options):
            # The hook sees the same tree and HEAD for a new commit and for an amend, but
            # an amend replaces HEAD. git accepts --amend abbreviated.
            return False
        if any(arg.startswith("-") and not arg.startswith("--") and "n" in arg[1:] for arg in options):
            return False  # -n is --no-verify for git commit
    return hooks.installed(lanes.project_root(cwd), match.git_hook)


def check_command(command, cwd, cfg, env, state, mode="run", consume=False):
    result = lanes.classify(command, cwd, cfg)
    chdir = _changes_directory(command)
    live, deferred_to_git = [], False
    for match in result.matches:
        if match.lane == 3 and match.git_hook and _git_hook_covers(match, command, cwd, chdir):
            deferred_to_git = True
            continue
        live.append(match)

    action = command_action(command, cwd)
    shown = f"`{action['command']}`"
    if mode == "hook" and any(m.lane == 3 for m in live) and RUN_PREFIX.match(command):
        # Approving here and again inside `babysitter run` would spend the approval
        # twice, so the hook sends the agent to the unwrapped command instead.
        outcome = Outcome(codes.APPROVAL_REQUIRED, result.lane, "approval-required",
                          f"{shown} is lane 3. This session is already governed by a hook, so run the "
                          "command directly without the `babysitter run --` wrapper; the hook will then "
                          "open an approval request for it.")
    elif any(m.lane == 3 for m in live):
        outcome = approval_outcome(state, action, cfg, env, consume, result.lane, shown)
    elif any(m.lane == 2 for m in live):
        seat = next(m.seat for m in live if m.lane == 2) or "code"
        outcome = consult_outcome(state, cfg, gate.action_digest(action), seat, result.lane, shown)
    elif deferred_to_git:
        outcome = Outcome(codes.OK, result.lane, "deferred-to-git-hook",
                          f"{shown}: our git hook will require an approval bound to the exact content.")
    else:
        outcome = Outcome(codes.OK, result.lane, "routine", f"{shown}: routine.")
    state.append("audit", {"command": command, "lane": result.lane, "status": outcome.status,
                           "code": outcome.code})
    return outcome


def _git(root, *args):
    return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
                          capture_output=True, timeout=60)


class CheckpointError(Exception):
    """The change set cannot be read. Callers fail closed: unknown is not routine."""


def _git_output(root, *args):
    done = _git(root, *args)
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise CheckpointError(f"cannot read the change set: git {args[0]} failed"
                              + (f" ({detail[0]})" if detail else ""))
    return done.stdout


def checkpoint(cwd, cfg, env, state):
    """Lane 2 when the finished change set is big or touches sensitive paths."""
    found = _git(cwd, "rev-parse", "--show-toplevel")
    if found.returncode != 0 or not found.stdout.strip():
        return Outcome(codes.OK, 1, "routine", "Not a git repository: no change set to check.")
    root = Path(found.stdout.decode("utf-8").strip())
    head = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    if head.returncode == 0:
        base = "HEAD"
    else:
        base = subprocess.run(["git", "-C", str(root), "hash-object", "-t", "tree", "--stdin"], input=b"",
                              capture_output=True, timeout=60).stdout.decode("ascii").strip()
    if not base:
        raise CheckpointError("cannot read the change set: git could not name a base to compare against")
    diff_args = ("diff", "--no-color", "--no-ext-diff", "--no-renames")
    patch = _git_output(root, *diff_args, "--binary", base)
    numstat = _git_output(root, *diff_args, "--numstat", "-z", base).decode("utf-8", "replace")
    untracked = [p for p in _git_output(root, "ls-files", "--others", "--exclude-standard", "-z")
                 .decode("utf-8", "replace").split("\0") if p]

    files, lines = [], 0
    for record in numstat.split("\0"):
        parts = record.split("\t", 2)
        if len(parts) == 3:
            files.append(parts[2])
            lines += sum(int(n) for n in parts[:2] if n.isdigit())
    digest = hashlib.sha256(patch)
    for path in sorted(untracked):
        try:
            content = (root / path).read_bytes()
        except OSError as exc:
            if not (root / path).is_symlink():  # a dangling link has no content to count
                raise CheckpointError(f"cannot read the change set: {path}: {exc}") from exc
            content = b""
        files.append(path)
        lines += content.count(b"\n") + (1 if content and not content.endswith(b"\n") else 0)
        digest.update(f"\0{path}\0{hashlib.sha256(content).hexdigest()}".encode("utf-8"))

    limits = cfg["changeset"]
    sensitive = sorted({f for f in files for pattern in limits["sensitive_paths"] if re.search(pattern, f)})
    reasons = []
    if len(files) > limits["max_files"]:
        reasons.append(f"{len(files)} files changed (limit {limits['max_files']})")
    if lines > limits["max_lines"]:
        reasons.append(f"{lines} lines changed (limit {limits['max_lines']})")
    if sensitive:
        reasons.append("sensitive paths: " + ", ".join(sensitive))
    if not reasons:
        return Outcome(codes.OK, 1, "routine", f"Change set is routine ({len(files)} files, {lines} lines).")
    what = "This change set (" + "; ".join(reasons) + ")"
    return consult_outcome(state, cfg, "changeset:" + digest.hexdigest(), "code", 2, what)
