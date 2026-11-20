#!/usr/bin/env bash
# Shared body of the fake advisor CLIs used by the test suite. Never shipped as
# a real advisor: it answers from STUB_MODE instead of calling a model.
#   verdict    a well-formed answer that starts with a VERDICT line
#   unicode    like verdict, with a non-ASCII check mark (U+2713) in the verdict line
#   narration  text with no verdict in it
#   empty      exit 0 with no output
#   fail       exit 1
stub_main() {
  local name="$1"; shift
  [ -n "${STUB_LOG:-}" ] && printf '%s\n' "$name" >> "$STUB_LOG"
  # With STUB_ARGS_LOG set, record the arguments, so a test can see the prompt it was given.
  [ -n "${STUB_ARGS_LOG:-}" ] && printf '%s\n' "$@" >> "$STUB_ARGS_LOG"
  # A codex-style CLI writes its final message to the file named by -o.
  local out="" args=("$@") i
  for ((i = 0; i < ${#args[@]}; i++)); do
    [ "${args[$i]}" = "-o" ] && out="${args[$((i + 1))]}"
  done
  local text=""
  case "${STUB_MODE:-verdict}" in
    verdict)   text=$'VERDICT: Option A is the safer choice.\n- It keeps the change reversible.' ;;
    unicode)   text="$(printf 'VERDICT: Option A \342\234\223 is the safer choice.\n- It keeps the change reversible.')" ;;
    narration) text="I'll inspect the repository and report back shortly." ;;
    empty)     text="" ;;
    fail)      echo "stub ${name}: simulated failure" >&2; exit 1 ;;
  esac
  if [ -n "$out" ]; then printf '%s' "$text" > "$out"; else printf '%s' "$text"; fi
}
