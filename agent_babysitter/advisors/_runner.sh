#!/usr/bin/env bash
# Run-and-guard helpers shared by the advisor wrappers. ask-claude.sh,
# ask-gemini.sh and ask-grok.sh use the full guarded run. ask-codex.sh uses only
# the portable timeout and the positive-integer check, and keeps its own output
# guard: it writes the final message with `-o` and treats a blank answer file as
# failure.
#
# SOURCED, never invoked directly. The read-only sandbox flags stay in the
# callers.
#
# Why this exists: the wrappers used to `exec` the CLI. `exec` replaces the
# shell, so nothing could inspect what happened afterwards. On 2026-08-11
# ask-codex.sh returned **exit 0 having produced zero bytes** after roughly 20
# minutes, and the calling agent had no way to tell that apart from a real
# review. A false green on an adversarial-review step is worse than a hard
# failure: it certifies unreviewed work as reviewed.
#
# Guarantees:
#   1. stdin is /dev/null, which is the actual fix for the observed hang.
#   2. A wall-clock timeout, portable across macOS and Linux.
#   3. Empty STDOUT is a FAILURE (exit 3), never a silent success.
#   4. The underlying CLI's exit code is preserved when it is non-zero.
#
# DESIGN NOTE, output is captured then emitted rather than streamed live. An
# earlier version teed through process substitution to keep live output. That
# reintroduced two hang risks in a script whose whole job is preventing hangs:
# a bare `wait` blocks on every child, and a surviving grandchild holding the
# pipe open stops tee from ever seeing EOF. Capturing to files has no pipe to
# hold open and no PIDs to wait on. The cost is that a long run prints nothing
# until it finishes, which is acceptable because callers redirect to a file and
# read it at the end anyway.
#
# KNOWN LIMITATION: exit codes 124, 137 and 142 are reported as timeouts even in the
# rare case the CLI itself chose to exit with one of them. Preferring a false
# "timed out" over a false "reviewed" is the safe direction for this tool.

# Resolve a timeout runner. Darwin has no `timeout`: coreutils installs it as
# `gtimeout`, and perl's alarm is the last resort since perl ships with macOS.
# --kill-after escalates to SIGKILL for a CLI that ignores SIGTERM.
_rr_run_with_timeout() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout --kill-after=10 "$secs" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout --kill-after=10 "$secs" "$@"
  elif command -v perl >/dev/null 2>&1; then
    # perl watchdog, not a bare `alarm; exec`. A bare alarm is defeated by a
    # SIGALRM the caller or the CLI ignores, and it never reaches grandchildren.
    # Here the CLI runs in its own process group. On timeout (or HUP/INT/TERM)
    # the handler sends TERM to the whole group, polls up to 10s for the leader
    # to exit, then sends KILL to the group. The kill must happen INSIDE the
    # handler: perl restarts an EINTR'd waitpid itself, so control never returns
    # to the caller of waitpid until the child is gone.
    perl -e '
      use POSIX ":sys_wait_h";
      my $secs = shift @ARGV;
      my $pid = fork();
      defined $pid or exit 127;
      if ($pid == 0) { setpgrp(0, 0); exec { $ARGV[0] } @ARGV; exit 127; }
      my $why = 0;
      # Set the group from the parent too, so a signal that lands before the child
      # calls setpgrp itself still has a group to reach. The leader is also
      # signalled directly: a CLI that moves itself into another group must not
      # outlive the cap.
      setpgrp($pid, $pid);
      my $stop = sub {
        my $gone = 0;
        kill "TERM", -$pid; kill "TERM", $pid;
        for (1 .. 50) { if (waitpid($pid, WNOHANG) != 0) { $gone = 1; last } select(undef, undef, undef, 0.2); }
        kill "KILL", -$pid; kill "KILL", $pid unless $gone;
      };
      $SIG{ALRM} = sub { $why = 124; $stop->() };
      $SIG{HUP}  = sub { $why = 129; $stop->() };
      $SIG{INT}  = sub { $why = 130; $stop->() };
      $SIG{TERM} = sub { $why = 143; $stop->() };
      alarm $secs;
      my $r = waitpid($pid, 0);
      my $st = $?;
      alarm 0;
      exit $why if $why;
      exit 127 if $r != $pid;
      exit(128 + ($st & 127)) if $st & 127;
      exit($st >> 8);
    ' "$secs" "$@"
  else
    # No way to enforce the cap: do not start a CLI that could then run forever.
    printf 'advisor wrapper: no timeout, gtimeout or perl on PATH; refusing to run without a time limit.
' >&2
    return 2
  fi
}

_rr_is_positive_int() {
  case "${1:-}" in
    ''|*[!0-9]*) return 1 ;;
    # At most 6 digits (~11 days). perl's alarm takes an unsigned int, so
    # 4294967296 would wrap to alarm(0), meaning NO timeout.
    ???????*) return 1 ;;
    *) [ "$1" -ge 1 ] 2>/dev/null || return 1 ;;
  esac
}

# _rr_guarded_run <label> <timeout_secs> <min_stdout_bytes> -- <command...>
_rr_guarded_run() {
  local label="$1" secs="$2" min_bytes="$3"
  shift 3
  [ "${1:-}" = "--" ] && shift

  # An unvalidated tunable silently DISABLES the guard: `[ 5 -lt bogus ]`
  # returns 2, and because it sits in an `if` condition `set -e` does not fire,
  # so the function would fall through and return 0. Fail closed instead.
  if ! _rr_is_positive_int "$secs"; then
    printf '%s: invalid timeout %s, expected a positive integer.\n' "$label" "${secs:-<empty>}" >&2
    return 2
  fi
  if ! _rr_is_positive_int "$min_bytes"; then
    printf '%s: invalid min-bytes %s, expected a positive integer.\n' "$label" "${min_bytes:-<empty>}" >&2
    return 2
  fi

  local out err status bytes
  out="$(mktemp "${TMPDIR:-/tmp}/${label}.out.XXXXXX")" || return 2
  err="$(mktemp "${TMPDIR:-/tmp}/${label}.err.XXXXXX")" || return 2

  # Cleanup via a function reading globals, so no path is interpolated into
  # trap code. A path containing an apostrophe would otherwise break quoting.
  _RR_OUT="$out" _RR_ERR="$err"
  _rr_cleanup() { rm -f "$_RR_OUT" "$_RR_ERR"; }
  # A signal must END the run with its conventional code. Cleanup alone would
  # let execution fall through after HUP/INT/TERM.
  _rr_on_signal() {
    _rr_cleanup
    trap - RETURN EXIT HUP INT TERM
    exit "$1"
  }
  trap _rr_cleanup RETURN EXIT
  trap '_rr_on_signal 129' HUP
  trap '_rr_on_signal 130' INT
  trap '_rr_on_signal 143' TERM

  # stdin from /dev/null is the ROOT-CAUSE FIX. Codex logs "Reading additional
  # input from stdin..." and BLOCKS FOREVER when stdin stays open, which is what
  # happens when an agent harness invokes the wrapper. Measured on one prompt:
  # stdin open hung past 20 minutes at zero bytes, `</dev/null` returned in 5
  # seconds. Do not remove this to "let the CLI read a piped prompt"; the prompt
  # is passed as an argument.
  set +e
  _rr_run_with_timeout "$secs" "$@" </dev/null >"$out" 2>"$err"
  status=$?
  set -e

  cat "$out"
  cat "$err" >&2

  if [ "$status" -eq 124 ] || [ "$status" -eq 137 ] || [ "$status" -eq 142 ]; then
    printf '%s: TIMED OUT after %ss. No review was produced.\n' "$label" "$secs" >&2
    return 124
  fi

  # Count STDOUT ONLY. The answer goes to stdout; banners, progress logging and
  # telemetry warnings go to stderr (verified against codex 0.145.0). Counting
  # both would let a banner-plus-nothing run pass the guard, which is the exact
  # false green this helper exists to prevent.
  # Printable bytes only ([:graph:] in the C locale): stdout that is only
  # whitespace, NULs or invisible Unicode (a zero-width space) is not a review.
  bytes=$(LC_ALL=C tr -cd '[:graph:]' <"$out" | wc -c | tr -d '[:space:]')

  if [ "$status" -ne 0 ]; then
    printf '%s: exited %s after producing %s printable stdout bytes.\n' "$label" "$status" "$bytes" >&2
    return "$status"
  fi

  if [ "$bytes" -lt "$min_bytes" ]; then
    printf '%s: FAILED. Exit code was 0 but stdout held only %s printable bytes (minimum %s).\n' \
      "$label" "$bytes" "$min_bytes" >&2
    printf '%s: treat this as NO REVIEW, not a clean pass.\n' "$label" >&2
    return 3
  fi

  return 0
}

# _rr_family_guard <label> <advisor family>
# Refuses a consult when the builder's model family (BABYSITTER_HOST_FAMILY) is
# unknown (return 6) or is the advisor's own family (return 7). A same-family
# second opinion shares the builder's blind spots, so it does not count.
_rr_family_guard() {
  local label="$1" advisor="$2" host="${BABYSITTER_HOST_FAMILY:-}"
  case "$host" in
    claude|gemini|codex|grok) ;;
    *)
      printf '%s: builder family unknown (BABYSITTER_HOST_FAMILY=%s); refusing to consult.\n' "$label" "${host:-<unset>}" >&2
      return 6
      ;;
  esac
  if [ "$host" = "$advisor" ]; then
    printf '%s: the builder is itself %s; a same-family consult is blocked.\n' "$label" "$advisor" >&2
    return 7
  fi
  return 0
}
