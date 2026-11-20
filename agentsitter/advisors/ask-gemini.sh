#!/usr/bin/env bash
# Read-only, non-interactive Gemini advisor wrapper (Antigravity CLI by default).
# Usage:
#   ask-gemini.sh "<prompt>" [cwd] [model]
#   ask-gemini.sh --prompt-file "<path>" [cwd] [model]
#
# Guarantees:
#   - Recursion guard via ADVISOR_DEPTH (exits 4 if called inside an advisor).
#   - Family guard: refuses when the builder is itself Gemini (exit 7) or when
#     the builder family is unknown (exit 6).
#   - Requires an explicit Gemini model id (third argument or ASK_GEMINI_MODEL)
#     and rejects anything that is not gemini-*. The host CLI can serve several
#     model families, so it must never silently answer with another one.
#   - Locked down with --sandbox --mode plan (note: do not pass
#     --disable-slash-commands, which disables --mode plan in agy v1.5+).
#   - Closed stdin (</dev/null), wall-clock timeout, automatic file-read routing
#     above 24 KB on Windows to avoid CreateProcess exit 126, and false-green
#     empty-stdout guard (exits 3 on blank output).
#
# Tunables:
#   ASK_GEMINI_CLI        CLI binary to run (default: agy)
#   ASK_GEMINI_MODEL      model id when no third argument is given
#   ASK_GEMINI_TIMEOUT    wall-clock seconds (default 360)
#   ASK_GEMINI_MIN_BYTES  minimum printable stdout bytes (default 1)

set -euo pipefail

# Digits only. Bash evaluates an arithmetic value as an expression, so anything
# else could run a command here, and a negative depth would defeat the check.
case "${ADVISOR_DEPTH:-0}" in
  ''|*[!0-9]*)
    printf 'ask-gemini: invalid ADVISOR_DEPTH=%s (expected nonnegative integer).
' "${ADVISOR_DEPTH:-}" >&2
    exit 2
    ;;
esac
# Compared as text: a number too large for the shell makes -ge fail, and a failed
# comparison must not read as depth zero.
if [ "${ADVISOR_DEPTH:-0}" != "0" ]; then
  printf 'ask-gemini: recursive advisor call blocked (ADVISOR_DEPTH=%s).\n' "${ADVISOR_DEPTH}" >&2
  exit 4
fi
export ADVISOR_DEPTH=1

# shellcheck source=_runner.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_runner.sh"
_rr_family_guard "ask-gemini" "gemini" || exit $?

to_native_path() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$1"
  else
    printf '%s\n' "$1"
  fi
}

EXTRA_ADD_DIRS=()
if [ "${1:-}" = "--prompt-file" ]; then
  PROMPT_FILE="${2:?usage: ask-gemini.sh --prompt-file <path> [cwd] [model]}"
  if [ ! -f "$PROMPT_FILE" ]; then
    printf 'ask-gemini: prompt file not found: %s\n' "$PROMPT_FILE" >&2
    exit 2
  fi
  if [ ! -s "$PROMPT_FILE" ]; then
    printf 'ask-gemini: prompt file is empty: %s\n' "$PROMPT_FILE" >&2
    exit 2
  fi
  CWD="${3:-$PWD}"
  MODEL="${4:-${ASK_GEMINI_MODEL:-}}"
  CWD_NATIVE="$(to_native_path "$CWD")"
  PROMPT_BYTES=$(wc -c < "$PROMPT_FILE" | tr -d '[:space:]')
  if [ "${PROMPT_BYTES:-0}" -gt 24000 ]; then
    PROMPT_DIR_ABS="$(cd "$(dirname "$PROMPT_FILE")" && pwd)"
    PROMPT_FILE_ABS="${PROMPT_DIR_ABS}/$(basename "$PROMPT_FILE")"
    PROMPT_DIR_NATIVE="$(to_native_path "$PROMPT_DIR_ABS")"
    PROMPT_FILE_NATIVE="$(to_native_path "$PROMPT_FILE_ABS")"
    EXTRA_ADD_DIRS+=( --add-dir "$PROMPT_DIR_NATIVE" )
    PROMPT="Read the prompt file at ${PROMPT_FILE_NATIVE} in full using read_file (if truncated at the per-call limit, read subsequent line or byte ranges until EOF so nothing is missed). Follow the instructions inside that file and answer from the evidence provided, using ${CWD_NATIVE} as the workspace directory."
  else
    PROMPT="$(cat < "$PROMPT_FILE")"
  fi
else
  PROMPT="${1:?usage: ask-gemini.sh \"<prompt>\" [cwd] [model]}"
  CWD="${2:-$PWD}"
  MODEL="${3:-${ASK_GEMINI_MODEL:-}}"
  CWD_NATIVE="$(to_native_path "$CWD")"
fi

case "$MODEL" in
  gemini-*) ;;
  '')
    printf 'ask-gemini: no model given. Pass a gemini-* model id as the third argument or set ASK_GEMINI_MODEL (config: advisors.gemini.model).\n' >&2
    exit 2
    ;;
  *)
    printf 'ask-gemini: model "%s" is not a Gemini model; a gemini-* model id is required.\n' "$MODEL" >&2
    exit 2
    ;;
esac

TIMEOUT_SECS="${ASK_GEMINI_TIMEOUT:-360}"

_rr_guarded_run "ask-gemini" "$TIMEOUT_SECS" "${ASK_GEMINI_MIN_BYTES:-1}" -- \
  "${ASK_GEMINI_CLI:-agy}" \
    --sandbox \
    --mode plan \
    --model "$MODEL" \
    --print-timeout "${TIMEOUT_SECS}s" \
    --add-dir "$CWD_NATIVE" \
    ${EXTRA_ADD_DIRS[@]+"${EXTRA_ADD_DIRS[@]}"} \
    -p "$PROMPT"
