#!/usr/bin/env bash
# Read-only, non-interactive Claude Code CLI advisor wrapper.
# Usage:
#   ask-claude.sh "<prompt>" [cwd]
#   ask-claude.sh --prompt-file "<path>" [cwd]
#
# Guarantees:
#   - Recursion guard via ADVISOR_DEPTH (exits 4 if called inside an advisor).
#   - Family guard: refuses when the builder is itself Claude (exit 7) or when
#     the builder family is unknown (exit 6).
#   - Locked down with --permission-mode plan --restricted --no-session-persistence
#     --strict-mcp-config (no shell/code execution, no user/project hooks, no disk clutter).
#   - Closed stdin (</dev/null), wall-clock timeout (default 180s), and false-green
#     empty-stdout guard (exits 3 on blank output) via _runner.sh.
#
# Tunables:
#   ASK_CLAUDE_MODEL      passed to --model (default: the CLI's own default)
#   ASK_CLAUDE_TIMEOUT    wall-clock seconds (default 180)
#   ASK_CLAUDE_MIN_BYTES  minimum printable stdout bytes (default 1)

set -euo pipefail

# Digits only. Bash evaluates an arithmetic value as an expression, so anything
# else could run a command here, and a negative depth would defeat the check.
case "${ADVISOR_DEPTH:-0}" in
  ''|*[!0-9]*)
    printf 'ask-claude: invalid ADVISOR_DEPTH=%s (expected nonnegative integer).
' "${ADVISOR_DEPTH:-}" >&2
    exit 2
    ;;
esac
# Compared as text: a number too large for the shell makes -ge fail, and a failed
# comparison must not read as depth zero.
if [ "${ADVISOR_DEPTH:-0}" != "0" ]; then
  printf 'ask-claude: recursive advisor call blocked (ADVISOR_DEPTH=%s).\n' "${ADVISOR_DEPTH}" >&2
  exit 4
fi
export ADVISOR_DEPTH=1

# shellcheck source=_runner.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_runner.sh"
_rr_family_guard "ask-claude" "claude" || exit $?

if [ "${1:-}" = "--prompt-file" ]; then
  PROMPT_FILE="${2:?usage: ask-claude.sh --prompt-file <path> [cwd]}"
  if [ ! -f "$PROMPT_FILE" ]; then
    printf 'ask-claude: prompt file not found: %s\n' "$PROMPT_FILE" >&2
    exit 2
  fi
  PROMPT="$(cat < "$PROMPT_FILE")"
  CWD="${3:-$PWD}"
else
  PROMPT="${1:?usage: ask-claude.sh \"<prompt>\" [cwd]}"
  CWD="${2:-$PWD}"
fi

MODEL_ARGS=()
[ -n "${ASK_CLAUDE_MODEL:-}" ] && MODEL_ARGS=( --model "$ASK_CLAUDE_MODEL" )

_rr_guarded_run "ask-claude" "${ASK_CLAUDE_TIMEOUT:-180}" "${ASK_CLAUDE_MIN_BYTES:-1}" -- \
  claude -p \
    --permission-mode plan \
    --restricted \
    --no-session-persistence \
    --strict-mcp-config \
    ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} \
    --add-dir "$CWD" \
    -- "$PROMPT"
