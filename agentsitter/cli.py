"""Command-line entry point: `python -m agentsitter <command> ...`."""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from . import codes, config, consult, enforce, gate, hooks, lanes, state


def _fail(message, code):
    print(f"agentsitter: {message}", file=sys.stderr)
    return code


def _state(cwd=None):
    return state.for_project(lanes.project_root(cwd or os.getcwd()), os.environ)


OPERATOR_WORD = re.compile(r"^\d*[&|;<>()]+[\d&-]*$")


def _command_words(words, name):
    """The shell command line to classify, approve and run.

    One argument is taken as a command line, exactly as written. Several
    arguments are separate words and stay separate: each is quoted for the
    shell, except a bare operator such as `&&` or `|`, which keeps its meaning.
    """
    words = list(words)
    if words and words[0] == "--":
        words = words[1:]
    if not words:
        raise UsageError(f"{name} needs a command after --")
    if len(words) == 1:
        return words[0]
    return " ".join(word if OPERATOR_WORD.match(word) else shlex.quote(word) for word in words)


class UsageError(Exception):
    pass


def _print_outcome(outcome, as_json, stream=None):
    if as_json:
        print(json.dumps(outcome.as_dict(), sort_keys=True))
    else:
        print(outcome.message, file=stream or sys.stdout)


def cmd_consult(args):
    cfg = config.load(os.environ)
    cwd = Path(args.cwd or os.getcwd())
    try:
        brief = Path(args.brief_file).read_text(encoding="utf-8")
    except OSError as exc:
        return _fail(f"cannot read brief file: {exc}", codes.USAGE)
    st = state.for_project(lanes.project_root(cwd), os.environ)
    result = consult.consult(st, cfg, os.environ, args.decision, args.seat, brief, cwd, advisor=args.advisor)
    if result.output:
        sys.stdout.write(result.output if result.output.endswith("\n") else result.output + "\n")
    if result.status == "failed" and result.stderr:
        sys.stderr.write(result.stderr if result.stderr.endswith("\n") else result.stderr + "\n")
    print(f"Advisor: {args.seat} via {result.advisor} | Verdict: {result.verdict or result.status} | "
          "Decide, verify against the files, and log your action.", file=sys.stderr)
    return result.code


def cmd_classify(args):
    command = _command_words(args.command, "classify")
    cfg = config.load(os.environ)
    result = lanes.classify(command, os.getcwd(), cfg)
    print(result.lane)
    for match in result.matches:
        print(f"{match.rule_id}\t{match.category}")
    return codes.OK


def cmd_check(args):
    command = _command_words(args.command, "check")
    cfg = config.load(os.environ)
    outcome = enforce.check_command(command, os.getcwd(), cfg, os.environ, _state(), mode="run", consume=False)
    _print_outcome(outcome, args.json)
    return outcome.code


def cmd_run(args):
    command = _command_words(args.command, "run")
    cfg = config.load(os.environ)
    cwd, st = os.getcwd(), _state()
    # Look first without spending anything. A blocked command is reported as blocked.
    outcome = enforce.check_command(command, cwd, cfg, os.environ, st, mode="run", consume=False)
    if outcome.code != codes.OK:
        return _fail(outcome.message, outcome.code)
    # An approval works once, so prove bash starts before the approval is used up.
    bash = None
    try:
        bash = consult.find_bash(cfg, os.environ)
        probe = subprocess.run([bash, "-c", "exit 0"], stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    except consult.ConsultError as exc:
        return _fail(f"{exc}. Nothing ran and no approval was used.", codes.USAGE)
    except (OSError, subprocess.SubprocessError) as exc:
        return _fail(f"bash could not be started ({bash}): {exc}. Nothing ran and no approval was used.",
                     codes.USAGE)
    if probe.returncode != 0:
        return _fail(f"bash did not run a trivial command ({bash}, exit {probe.returncode}). "
                     "Nothing ran and no approval was used.", codes.USAGE)
    outcome = enforce.check_command(command, cwd, cfg, os.environ, st, mode="run", consume=True)
    if outcome.code != codes.OK:
        return _fail(outcome.message, outcome.code)
    try:
        return subprocess.run([bash, "-c", command]).returncode
    except OSError as exc:
        return _fail(f"bash could not be started ({bash}): {exc}. The command did not run; "
                     "any approval it relied on has been used.", codes.USAGE)


def cmd_checkpoint(args):
    cfg = config.load(os.environ)
    outcome = enforce.checkpoint(os.getcwd(), cfg, os.environ, _state())
    _print_outcome(outcome, args.json)
    return outcome.code


def summarize(action):
    kind = action.get("kind")
    if kind == "command":
        return f"{action.get('command')}  (in {action.get('cwd')})"
    if kind in ("git-commit", "git-merge-commit"):
        return f"commit tree {str(action.get('tree'))[:12]} on {str(action.get('parent'))[:12]} in {action.get('repo')}"
    if kind == "git-push":
        refs = ", ".join(f"{u[0]} -> {u[2]}" for u in action.get("updates", []) if len(u) >= 3)
        return f"push to {action.get('remote')}: {refs}"
    if kind == "git-rebase":
        return f"rebase {action.get('branch')} onto {action.get('upstream')} in {action.get('repo')}"
    if kind == "edit":
        return f"{action.get('tool')} {action.get('path')}"
    if kind == "budget-reset":
        return f"reset the consult budget ({action.get('consults')} consults so far)"
    return json.dumps(action, sort_keys=True)[:200]


def cmd_requests(args):
    items = [{"id": rid, "kind": doc["action"].get("kind", "?"), "summary": summarize(doc["action"])}
             for rid, doc in gate.pending(_state())]
    if args.json:
        print(json.dumps(items))
    elif not items:
        print("No pending requests.")
    else:
        for item in items:
            print(f"{item['id']}  {item['kind']}  {item['summary']}")
    return codes.OK


def _agent_session_reason(cfg):
    try:
        depth = int(os.environ.get("ADVISOR_DEPTH", "0") or "0")
    except ValueError:
        depth = 1
    if depth >= 1:
        return "ADVISOR_DEPTH is set: this is an advisor process"
    markers = [name for name in cfg["family_markers"] if os.environ.get(name)]
    if markers:
        return f"agent session marker {', '.join(sorted(markers))} is set"
    try:
        tty = sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        tty = False
    if not tty:
        return "stdin is not a terminal"
    return ""


def cmd_approve(args):
    cfg = config.load(os.environ)
    refusal = _agent_session_reason(cfg)
    if refusal:
        return _fail(f"approve refused: {refusal}. Approvals are signed by a human in their own terminal; "
                     "nothing was signed.", codes.APPROVE_REFUSED)
    return approve_request(_state(), cfg, args.request_id, args.key)


def approve_request(st, cfg, request_id, key=None, confirm=input, out=None, err=None):
    """Show one pending request, ask for a `y`, and sign it. Returns the exit code.

    The caller has already refused agent sessions; this function never checks.
    """
    out = out or sys.stdout
    err = err or sys.stderr

    def fail(message, code):
        print(f"agentsitter: {message}", file=err)
        return code

    try:
        request = gate.load_request(st, request_id)
    except gate.GateError as exc:
        return fail(str(exc), codes.APPROVAL_INVALID)
    action = request["action"]
    print(f"Request {request_id}", file=out)
    for name in sorted(action):
        print(f"  {name}: {json.dumps(action[name])}", file=out)
    detail = request.get("detail")
    if isinstance(detail, dict):
        # The action names an edit by hash; this is the edit, checked against that hash.
        print("  the edit itself:", file=out)
        for name in sorted(detail):
            print(f"    {name}: {json.dumps(detail[name])}", file=out)
    if action.get("kind") == "git-commit" and action.get("repo"):
        staged = subprocess.run(["git", "-C", action["repo"], "write-tree"], capture_output=True, text=True)
        if staged.returncode == 0 and staged.stdout.strip() == action.get("tree"):
            stat = subprocess.run(["git", "-C", action["repo"], "diff", "--cached", "--stat"],
                                  capture_output=True, text=True)
            print(stat.stdout.rstrip(), file=out)
    key = key or cfg["approver"].get("signing_key")
    if not key:
        return fail("no signing key: pass --key or set approver.signing_key in the config", codes.USAGE)
    answer = confirm("Sign this approval? [y/N] ").strip().lower()
    if answer != "y":
        print("Not signed.", file=out)
        return codes.APPROVE_REFUSED
    try:
        gate.sign(st, request_id, key, cfg, interactive=True)
    except gate.GateError as exc:
        return fail(str(exc), codes.APPROVAL_INVALID)
    print(f"Signed approval for request {request_id}.", file=out)
    return codes.OK


def cmd_budget(args):
    cfg = config.load(os.environ)
    st = _state()
    consults = len(st.records("consults"))
    action = {"kind": "budget-reset", "project": st.root.name, "consults": consults}
    outcome = enforce.approval_outcome(st, action, cfg, os.environ, True, 3, "Resetting the consult budget")
    if outcome.code != codes.OK:
        return _fail(outcome.message, outcome.code)
    st.append("budget-resets", {"consults_before": consults})
    print("Consult budget reset.")
    return codes.OK


def cmd_install_hooks(args):
    repo = Path(args.repo or os.getcwd())
    written = hooks.install(lanes.project_root(repo), force=args.force)
    for path in written:
        print(f"wrote {path.as_posix()}")
    return codes.OK


def cmd_status(args):
    cfg = config.load(os.environ)
    st = _state()
    try:
        family = consult.host_family(cfg, os.environ)
    except consult.ConsultError:
        family = "unknown: consults will fail closed"
    root = lanes.project_root(os.getcwd())
    budget = cfg["budget"]
    print(f"builder family: {family}")
    print(f"consults this window: {len(consult.window(st))} of {budget['per_window']} "
          f"(per decision: {budget['per_decision']})")
    present = [name for name in hooks.HOOK_NAMES if hooks.installed(root, name)]
    print(f"git hooks installed: {', '.join(present) if present else 'none'}")
    old = [name for name in hooks.HOOK_NAMES if hooks.stale(root, name)]
    if old:
        print(f"git hooks to reinstall (written by another interpreter or release): {', '.join(old)}")
    anchor = config.allowed_signers_path(cfg, os.environ)
    print(f"trust anchor: {anchor.as_posix()} ({'present' if anchor.is_file() else 'missing'})")
    print(f"pending requests: {len(gate.pending(st))}")
    return codes.OK


def cmd_hook(args):
    if args.hook_kind == "agent":
        try:
            cfg = config.load(os.environ)
            return hooks.agent_hook(sys.stdin.read(), cfg, os.environ)
        except Exception as exc:  # fail closed: any error blocks the tool call
            print(f"agentsitter: hook error ({exc}); blocking.", file=sys.stderr)
            return 2
    cfg = config.load(os.environ)
    stdin_text = sys.stdin.read() if args.name == "pre-push" else ""
    return hooks.git_hook(args.name, args.args, stdin_text, cfg, os.environ)


def build_parser():
    parser = argparse.ArgumentParser(prog="agentsitter", description="Three-lane governance for coding agents.")
    sub = parser.add_subparsers(dest="command_name", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("consult", help="run one cross-family advisor consult")
    p.add_argument("--decision", required=True, help="stable id of the decision being consulted on")
    p.add_argument("--seat", required=True, choices=config.SEATS)
    p.add_argument("--brief-file", required=True, help="file holding the question and its context")
    p.add_argument("--advisor", help="advisor family, overriding the seat routing")
    p.add_argument("--cwd", help="directory the advisor may read (default: current directory)")
    p.set_defaults(func=cmd_consult)

    p = sub.add_parser("classify", help="print the lane of a shell command")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- <command...>")
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("check", help="say whether a command may run now, and what to do if not")
    p.add_argument("--json", action="store_true")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- <command...>")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("run", help="run a command only if its lane allows it")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- <command...>")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("checkpoint", help="check the finished change set of this repository")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_checkpoint)

    p = sub.add_parser("requests", help="list pending approval requests")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_requests)

    p = sub.add_parser("approve", help="(approver only) sign one pending request in your own terminal")
    p.add_argument("request_id")
    p.add_argument("--key", help="private signing key (default: approver.signing_key)")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("budget", help="manage the consult budget")
    p.add_argument("budget_action", choices=["reset"])
    p.set_defaults(func=cmd_budget)

    p = sub.add_parser("install-hooks", help="install the git hooks into a repository")
    p.add_argument("--repo", help="repository (default: current directory)")
    p.add_argument("--force", action="store_true", help="replace hooks not written by agentsitter")
    p.set_defaults(func=cmd_install_hooks)

    p = sub.add_parser("status", help="show governance status for this project")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("hook", help="entry points called by git and agent harnesses")
    hook_sub = p.add_subparsers(dest="hook_kind", metavar="KIND")
    hook_sub.required = True
    g = hook_sub.add_parser("git", help="called by an installed git hook")
    g.add_argument("name", choices=hooks.HOOK_NAMES)
    g.add_argument("args", nargs=argparse.REMAINDER)
    g.set_defaults(func=cmd_hook)
    a = hook_sub.add_parser("agent", help="called by an agent harness (JSON on stdin)")
    a.set_defaults(func=cmd_hook)
    return parser


def _utf8_streams():
    """Emit UTF-8 on stdout and stderr whatever the console code page is. Never raises."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # a stream that cannot switch keeps its encoding
            pass


def main(argv=None):
    _utf8_streams()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UsageError as exc:
        return _fail(str(exc), codes.USAGE)
    except hooks.HookError as exc:
        return _fail(str(exc), codes.USAGE)
    except consult.ConsultError as exc:
        return _fail(str(exc), exc.code)
    except config.ConfigError as exc:
        return _fail(str(exc), codes.USAGE)
    except enforce.CheckpointError as exc:
        return _fail(str(exc), codes.USAGE)
