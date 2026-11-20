#!/usr/bin/env bash
# Read-only, non-interactive Grok CLI reviewer (no shell, no MCP, reads in cwd).
# Usage: ask-grok.sh "<prompt>" [cwd]
#
# Locked down so this can be permission-allowlisted and called from unattended
# loops without mutating files, reaching the network or reading secrets:
#   --tools read_file,grep,list_dir   only read tools: no shell, no writer
#   --disallowed-tools search_tool,use_tool and --deny 'MCPTool(*)'
#                                     no MCP gateway
#   --sandbox strict                  reads confined to the cwd (plus system
#                                     paths), so a prompt-injected reviewer
#                                     cannot read secrets elsewhere in the
#                                     home directory
#   --no-subagents                    one review pass, no agent tree
# A Grok config may run always-approve with catch-all allow rules, and
# allow rules apply on top of any --permission-mode, so the tool list is what
# holds. Measured 2026-10-27: before this, a reviewer here (then
# --sandbox read-only) ran `curl` to the internet with no prompt; under strict,
# reads outside the cwd return "Permission denied". Denied calls fail cleanly,
# with no approval prompt and no hang. Paste diffs into the prompt and keep
# context files inside the cwd; the reviewer cannot run git.
#
# Exit codes:
#   0   review produced
#   2   an invalid tunable: the guard refused to run
#   3   FALSE GREEN GUARD: grok exited 0 but stdout was blank or under the minimum
#   4   recursive advisor call blocked (ADVISOR_DEPTH >= 1)
#   5   login check failed: no Grok login, or it expires before this call's
#       timeout ends. The strict sandbox cannot refresh a login, so without
#       this check grok retried 401s until the timeout (seen 2026-11-11).
#       Sign in again yourself (run `grok` interactively once) or use the
#       next advisor.
#   6   builder family unknown (AGENTSITTER_HOST_FAMILY unset or not recognised)
#   7   the builder is itself Grok (same-family consult blocked)
#   124 timed out, no review produced
#   *   grok's own non-zero exit, passed through
#
# Tunables:
#   ASK_GROK_TIMEOUT     wall-clock seconds (default 900; 1-6 digits)
#   ASK_GROK_MIN_BYTES   minimum NON-WHITESPACE stdout bytes (default 1); stderr never counts
#   ASK_GROK_AUTH_FILE   login file the preflight reads (default ~/.grok/auth.json)
#
# Login preflight: reads ONLY the `expires_at` values of the login file (never
# prints, copies or sends anything else from it), uses no network, never
# refreshes credentials and leaves the sandbox as is. It needs the latest
# expiry to be later than now + the timeout + 60 s; otherwise exit 5 at once.
set -euo pipefail

case "${ADVISOR_DEPTH:-0}" in
  ''|*[!0-9]*)
    printf 'ask-grok: invalid ADVISOR_DEPTH=%s (expected nonnegative integer).\n' "${ADVISOR_DEPTH:-}" >&2
    exit 2
    ;;
esac
# Compared as text: a number too large for the shell makes -ge fail, and a failed
# comparison must not read as depth zero.
if [ "${ADVISOR_DEPTH:-0}" != "0" ]; then
  printf 'ask-grok: recursive advisor call blocked (ADVISOR_DEPTH=%s).\n' "${ADVISOR_DEPTH}" >&2
  exit 4
fi
export ADVISOR_DEPTH=1

# shellcheck source=_runner.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_runner.sh"
_rr_family_guard "ask-grok" "grok" || exit $?

if [ "${1:-}" = "--prompt-file" ]; then
  PROMPT_FILE="${2:?usage: ask-grok.sh --prompt-file <path> [cwd]}"
  if [ ! -f "$PROMPT_FILE" ]; then
    printf 'ask-grok: prompt file not found: %s\n' "$PROMPT_FILE" >&2
    exit 2
  fi
  if ! PROMPT="$(cat < "$PROMPT_FILE")"; then
    printf 'ask-grok: failed to read prompt file: %s\n' "$PROMPT_FILE" >&2
    exit 2
  fi
  if [ -z "$PROMPT" ]; then
    printf 'ask-grok: prompt file is empty: %s\n' "$PROMPT_FILE" >&2
    exit 2
  fi
  CWD="${3:-$PWD}"
else
  PROMPT="${1:?usage: ask-grok.sh \"<prompt>\" [cwd]}"
  CWD="${2:-$PWD}"
fi

TIMEOUT_S="${ASK_GROK_TIMEOUT:-900}"
case "$TIMEOUT_S" in
  ''|*[!0-9]*) ;;  # invalid: the runner below refuses it with exit 2
  *)
    AUTH_FILE="${ASK_GROK_AUTH_FILE:-$HOME/.grok/auth.json}"
    auth_fail() {
      printf 'ask-grok: %s The sandboxed reviewer cannot refresh a login. Sign in again yourself (run `grok` interactively once), then retry, or use the next advisor. No review was run.\n' "$1" >&2
      exit 5
    }
    [ -r "$AUTH_FILE" ] || auth_fail "No readable Grok login file ($AUTH_FILE)."
    LATEST=""
    # Only the expires_at timestamps are extracted; nothing else is read out.
    STAMPS="$(grep -oE '"expires_at"[[:space:]]*:[[:space:]]*"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[^"]*"' "$AUTH_FILE" 2>/dev/null \
      | sed -E 's/.*"([^"]+)"$/\1/' || true)"
    while IFS= read -r stamp; do
      [ -n "$stamp" ] || continue
      # 2026-11-11T23:18:57.718352Z -> epoch seconds (BSD date, else GNU date).
      base="${stamp%%.*}"; base="${base%Z}"
      epoch="$(date -j -u -f '%Y-%m-%dT%H:%M:%S' "$base" +%s 2>/dev/null || date -u -d "${base}Z" +%s 2>/dev/null || true)"
      case "$epoch" in ''|*[!0-9]*) continue ;; esac
      if [ -z "$LATEST" ] || [ "$epoch" -gt "$LATEST" ]; then LATEST="$epoch"; fi
    done <<< "$STAMPS"
    [ -n "$LATEST" ] || auth_fail "The Grok login file has no readable expiry, so the login cannot be verified."
    NOW="$(date -u +%s)"
    if [ "$LATEST" -le "$NOW" ]; then
      auth_fail "The Grok login has expired."
    elif [ "$LATEST" -lt $(( NOW + 10#$TIMEOUT_S + 60 )) ]; then
      auth_fail "The Grok login expires in $(( (LATEST - NOW) / 60 )) min, before this call's ${TIMEOUT_S}s timeout ends."
    fi
    ;;
esac

_rr_guarded_run "ask-grok" "$TIMEOUT_S" "${ASK_GROK_MIN_BYTES:-1}" -- \
  grok -p "$PROMPT" \
    --sandbox strict \
    --cwd "$CWD" \
    --no-subagents \
    --disable-web-search \
    --permission-mode default \
    --tools read_file,grep,list_dir \
    --disallowed-tools search_tool,use_tool \
    --deny 'MCPTool(*)'
