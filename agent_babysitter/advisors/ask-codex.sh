#!/usr/bin/env bash
# Read-only, non-interactive OpenAI Codex CLI reviewer.
#
# Usage: ask-codex.sh "<prompt>" [cwd] [output-schema.json]
#
# Prints ONLY the reviewer's final answer on success. Default runs are
# `codex exec --ephemeral`: no rollout is written, and there is no session-id
# footer because the run is not resumable. Set ASK_CODEX_PERSIST=1 to omit
# --ephemeral, persist a rollout, and print `[codex session <id>]`.
# Everything else Codex emits (banner, echoed prompt, skill-loader
# errors, tool transcripts, token counts) is captured to files and kept out of
# the caller's context -- measured 2026-09-04 at 163 bytes of answer against
# 2845 bytes of stream noise for the same review.
#
# Hard-codes --sandbox read-only so this can be permission-allowlisted and
# called from unattended loops without risking file mutation, and keeps every
# MCP server, ChatGPT connector, plugin and web search out of the run (LOCKDOWN
# below), because those run outside the sandbox.
#
# Exit codes: 0 = success, 1 = failure/empty, 2 = usage/env error, 4 = recursive call blocked,
# 6 = builder family unknown, 7 = the builder is itself Codex (same-family consult blocked).
#
# Env overrides:
#   ASK_CODEX_TIMEOUT   seconds before the call is killed (default 600); must be a
#                       positive integer of at most 6 digits, else exit 2
#   ASK_CODEX_MODEL     passed to codex -m (default: whatever config.toml sets)
#   ASK_CODEX_EFFORT    passed to codex -c model_reasoning_effort=... (default: whatever config.toml sets)
#   ASK_CODEX_PERSIST   set to 1 to persist a resumable session (omit --ephemeral
#                       and print the session-id footer). Default: ephemeral.
set -uo pipefail

case "${ADVISOR_DEPTH:-0}" in
  ''|*[!0-9]*)
    printf 'ask-codex: invalid ADVISOR_DEPTH=%s (expected nonnegative integer).\n' "${ADVISOR_DEPTH:-}" >&2
    exit 2
    ;;
esac
# Compared as text: a number too large for the shell makes -ge fail, and a failed
# comparison must not read as depth zero.
if [ "${ADVISOR_DEPTH:-0}" != "0" ]; then
  printf 'ask-codex: recursive advisor call blocked (ADVISOR_DEPTH=%s).\n' "${ADVISOR_DEPTH}" >&2
  exit 4
fi
export ADVISOR_DEPTH=1

PROMPT_FILE=""
PROMPT=""
if [ "${1:-}" = "--prompt-file" ]; then
  PROMPT_FILE="${2:?usage: ask-codex.sh --prompt-file <path> [cwd] [output-schema.json]}"
  if [ ! -f "$PROMPT_FILE" ]; then
    printf 'ask-codex: prompt file not found: %s\n' "$PROMPT_FILE" >&2
    exit 2
  fi
  if [ ! -r "$PROMPT_FILE" ]; then
    printf 'ask-codex: failed to read prompt file: %s\n' "$PROMPT_FILE" >&2
    exit 2
  fi
  if [ ! -s "$PROMPT_FILE" ]; then
    printf 'ask-codex: prompt file is empty: %s\n' "$PROMPT_FILE" >&2
    exit 2
  fi
  CWD="${3:-$PWD}"
  SCHEMA="${4:-}"
else
  PROMPT="${1:?usage: ask-codex.sh \"<prompt>\" [cwd] [output-schema.json]}"
  CWD="${2:-$PWD}"
  SCHEMA="${3:-}"
fi
TIMEOUT="${ASK_CODEX_TIMEOUT:-600}"

# shellcheck source=_runner.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_runner.sh"
_rr_family_guard "ask-codex" "codex" || exit $?

# Fail closed on a bad timeout. perl's alarm (the macOS fallback below) treats a
# non-number as 0, meaning NO alarm, so an unvalidated value would silently run
# unbounded.
if ! _rr_is_positive_int "$TIMEOUT"; then
  echo "ask-codex: invalid ASK_CODEX_TIMEOUT '$TIMEOUT', expected a positive integer." >&2
  exit 2
fi

# The read-only sandbox governs only codex's own shell. MCP servers, ChatGPT
# connectors (apps) and plugins run outside it: measured 2026-10-27, a run with
# --sandbox read-only alone listed node_repl plus GitHub, Gmail, Drive and
# Calendar tools (send, merge, delete), answered live connector reads with no
# prompt, and ran a live web search. --ignore-user-config skips
# ~/.codex/config.toml and, because project trust lives there, every project
# .codex/config.toml too (measured: a required project MCP server no longer
# loads), so no configured MCP server, plugin, notify hook or web-search
# setting reaches the reviewer. Connectors and the bundled browser, computer-use
# and install tools are on by default, so they are disabled by name.
LOCKDOWN=(
  --ignore-user-config
  -c web_search=disabled
  --disable apps
  --disable plugins
  --disable remote_plugin
  --disable browser_use
  --disable browser_use_external
  --disable computer_use
  --disable image_generation
  --disable skill_mcp_dependency_install
  --disable tool_suggest
  --disable memories
)

# --ignore-user-config also drops the user's default model and reasoning
# effort, so read those two top-level keys here and pass them back explicitly.
CODEX_CONFIG="${CODEX_HOME:-$HOME/.codex}/config.toml"
toml_top() {
  [ -f "$CODEX_CONFIG" ] || return 0
  sed -n "/^\[/q;s/^$1 *= *\"\([^\"]*\)\".*/\1/p" "$CODEX_CONFIG" | head -1
}
MODEL="${ASK_CODEX_MODEL:-$(toml_top model)}"
EFFORT="${ASK_CODEX_EFFORT:-$(toml_top model_reasoning_effort)}"
case "$EFFORT" in *[!a-z]*) EFFORT="" ;; esac

to_native_path() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$1"
  else
    printf '%s\n' "$1"
  fi
}

# A fresh private directory or nothing: a guessable fallback path could already
# exist and point somewhere else.
if ! RUN_DIR="$(mktemp -d)" || [ ! -d "$RUN_DIR" ]; then
  echo 'ask-codex: could not create a temporary directory.' >&2
  exit 2
fi
OUT="$RUN_DIR/answer.txt"
ERR="$RUN_DIR/codex.stderr"
: > "$OUT"

if [ -n "$PROMPT_FILE" ]; then
  PROMPT_IN="$PROMPT_FILE"
else
  PROMPT_IN="$RUN_DIR/prompt.txt"
  printf '%s' "$PROMPT" > "$PROMPT_IN"
fi
CWD_NATIVE="$(to_native_path "$CWD")"
OUT_NATIVE="$(to_native_path "$OUT")"
SCHEMA_NATIVE=""
[ -n "$SCHEMA" ] && SCHEMA_NATIVE="$(to_native_path "$SCHEMA")"

# True only when the answer file holds at least one printable, non-space byte.
# Plain `[ -s ]` is not enough: codex can exit 0 having written only newlines,
# which would otherwise be forwarded as a successful but blank review.
has_answer() { LC_ALL=C grep -q '[[:graph:]]' "$OUT" 2>/dev/null; }

# --- run one codex attempt -------------------------------------------------
# "$@" = extra args spliced in before the prompt (used by the MCP retry below).
run_codex() {
  local extra=( "$@" )
  # Default is a one-shot with no rollout. ASK_CODEX_PERSIST=1 keeps a
  # resumable session. This lives inside run_codex so a required-MCP retry
  # preserves the same persistence mode.
  local cmd=(
    codex
    -c features.prefer_mxc=false
    -c windows.sandbox=unelevated
    exec
  )
  case "${ASK_CODEX_PERSIST:-}" in
    1) ;;
    *) cmd+=( --ephemeral ) ;;
  esac
  cmd+=(
    --sandbox read-only
    --skip-git-repo-check
    "${LOCKDOWN[@]}"
    -C "$CWD_NATIVE"
    -o "$OUT_NATIVE"
  )
  [ -n "$SCHEMA_NATIVE" ] && cmd+=( --output-schema "$SCHEMA_NATIVE" )
  [ -n "$MODEL" ] && cmd+=( -m "$MODEL" )
  [ -n "$EFFORT" ] && cmd+=( -c "model_reasoning_effort=$EFFORT" )
  [ ${#extra[@]} -gt 0 ] && cmd+=( "${extra[@]}" )
  # `-- -` is BOTH a security boundary and the Windows 32 KB CreateProcess fix:
  # options end at `--`, `-` tells codex exec to read the prompt from stdin, and
  # `< "$PROMPT_IN"` feeds a finite disk file (deterministic EOF, never the
  # caller's open harness pipe, and unlimited prompt size on Windows).
  cmd+=( -- - )

  # File-backed stdin (`< "$PROMPT_IN"`) is LOAD-BEARING. Called with an open
  # harness pipe (Claude Code's Bash tool), codex waits on that pipe forever.
  # Redirecting from a regular file delivers the full prompt and immediate EOF.
  # _rr_run_with_timeout falls back from `timeout` to `gtimeout` to perl's alarm.
  _rr_run_with_timeout "$TIMEOUT" "${cmd[@]}" <"$PROMPT_IN" >/dev/null 2>"$ERR"
  local rc=$?
  # perl's alarm ends the run with SIGALRM (142); a SIGKILL escalation is 137.
  case $rc in 137|142) rc=124 ;; esac
  return "$rc"
}

run_codex
RC=$?

# --- retry once past a required-MCP-server abort ---------------------------
# Since 2026-10-27 --ignore-user-config keeps project config out, so this should
# not fire; it stays in case a codex release reads project config without trust.
# A project .codex/config.toml can declare an MCP server with required = true;
# if that server fails to hand-shake, codex aborts before ever reaching the
# model. Disable the servers it named and retry, so the review keeps its repo
# read access instead of being relocated to an unrelated cwd.
# The error names EVERY failed server as "name: reason; name: reason", so all of
# them must be disabled -- clearing only the first leaves the run still aborting.
# Never retry a timed-out run (124): its stderr can hold the prompt echo, and
# retrying would double the wall clock. Never retry a run ended by a signal
# (129/130/143): Ctrl-C must stop the review, not start a second one. Never retry
# when an answer already exists, and truncate the answer file before retrying, so
# a failed first attempt's answer can never be printed under the retry's exit 0.
# `tail -1` below takes the LAST match, because codex echoes the prompt into
# stderr before any real error.
if [ $RC -ne 0 ] && [ $RC -lt 124 ] && ! has_answer && grep -q "required MCP servers failed to initialize" "$ERR" 2>/dev/null; then
  BAD_ARGS=()
  while IFS= read -r name; do
    [ -n "$name" ] && BAD_ARGS+=( -c "mcp_servers.${name}.enabled=false" )
  done < <(
    # Names containing a dot are deliberately NOT matched. `-c` takes a dotted
    # path, so `mcp_servers.foo.bar.enabled=false` would address a nested key
    # under a server called `foo` rather than the server `foo.bar`. Quoting the
    # segment does not help: `mcp_servers."foo.bar".enabled=false` was tested
    # 2026-09-04 and defines a NEW, transport-less server instead of merging
    # into the existing one, which breaks the retry outright
    # (`Error loading config.toml: invalid transport`). Skipping such a name
    # means the retry does not cover it and the wrapper reports an honest
    # failure -- strictly better than emitting an override that is wrong.
    sed -n 's/.*required MCP servers failed to initialize: \(.*\)/\1/p' "$ERR" \
      | tail -1 | tr ';' '\n' \
      | sed -n 's/^ *\([A-Za-z0-9_-][A-Za-z0-9_-]*\) *:.*/\1/p' \
      | sort -u
  )
  if [ ${#BAD_ARGS[@]} -gt 0 ]; then
    echo "[ask-codex] required MCP server(s) failed to start; retrying with them disabled" >&2
    : > "$OUT"
    run_codex "${BAD_ARGS[@]}"
    RC=$?
  fi
fi

SESSION="$(sed -n 's/^session id: *//p' "$ERR" 2>/dev/null | head -1)"

# --- an empty answer is a FAILURE, never "no findings" ---------------------
if [ $RC -ne 0 ] || ! has_answer; then
  echo "ask-codex FAILED (exit $RC) -- this is NOT a clean 'no findings' result." >&2
  case $RC in
    124) echo "  cause: timed out after ${TIMEOUT}s (or hung on stdin)." >&2 ;;
  esac
  if grep -qi "out of credits" "$ERR" 2>/dev/null; then
    echo "  cause: OpenAI workspace is OUT OF CREDITS -- add credits, then retry." >&2
  fi
  if grep -q "required MCP servers failed to initialize" "$ERR" 2>/dev/null; then
    echo "  cause: a required MCP server would not start, and the retry did not clear it." >&2
  fi
  has_answer || echo "  the answer is empty: the model was never reached, or returned nothing." >&2
  echo "  full stderr: $ERR" >&2
  echo "  --- last 15 stderr lines ---" >&2
  tail -15 "$ERR" >&2
  # Never propagate 0 here. codex can exit 0 with an empty answer, and
  # `exit "${RC:-1}"` would expand to `exit 0` (the :- default applies only when
  # RC is unset or null, and "0" is neither) -- reporting success to automation
  # immediately after printing FAILED.
  [ "$RC" -eq 0 ] && RC=1
  exit "$RC"
fi

# The answer must actually reach stdout: a failed print is not a review.
cat < "$OUT" || exit 1
echo
if [ "${ASK_CODEX_PERSIST:-}" = "1" ] && [ -n "$SESSION" ]; then
  echo "[codex session $SESSION]"
fi
# A failed run keeps its directory, because the message above names the log in it.
# A good run has nothing left to read: do not leave the brief and the answer behind.
rm -rf "$RUN_DIR"
exit 0
