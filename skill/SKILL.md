---
name: babysitter
description: How to work under agent-babysitter governance - keep building, consult another model family on non-trivial decisions, and stop for a signed approval on irreversible ones.
---

# Working under agent-babysitter

You are a coding agent in a governed session. Keep working without waiting on
the approver for routine and non-trivial decisions. Only irreversible actions
wait for the approver, and even then you finish all the preparation first.

## The three lanes

- Lane 1, routine: reversible work such as reading, editing source, running
  tests, `git status`, deleting scratch output. It runs.
- Lane 2, non-trivial: dependency changes, schema migrations you author, a big
  or sensitive change set. It runs once one consult with an advisor from a
  different model family is on record for that exact decision.
- Lane 3, irreversible or outward-facing: commits, pushes, merges, publishes,
  deploys, external writes, secret changes, deletion outside scratch space,
  edits to governance files. It runs only on a signed approval from the
  approver for that exact action. Each approval works once.

If a harness hook is installed, your commands are checked as you run them.
Run them directly; do not wrap them in `babysitter run --`. Without a hook,
use `babysitter check -- <command>` or `babysitter run -- <command>`.

## What to do on each exit code

- 0: go ahead.
- 10 (consult required): write a compact brief to a file, then run the exact
  `babysitter consult --decision <id> --seat <seat> --brief-file <file>`
  line from the message. Read the verdict, verify it against the real files,
  decide, and retry the original command.
- 11 (consult failed): retry the consult once. If it fails again, the fallback
  applies: the command is allowed and you proceed on your own judgment, which
  is logged.
- 12 (budget exhausted): stop. Report your progress to the approver, who can
  reset the budget.
- 13 or 14 (own family, or family unknown): fix the family setting
  (`BABYSITTER_HOST_FAMILY` must name your own family). Never consult your
  own family, and never pick a family that is not yours to get past this.
- 20 or 21 (approval required or invalid): finish every preparation step you
  can, then stop. Present a readiness summary to the approver: what the action
  is, why it is needed, what you checked, and the request id from the message.
  Retry the identical action after the approver signs.

Never try to approve, sign, bypass hooks, use `--no-verify`, change
`BABYSITTER_*` variables, or edit the config, the allowed-signers file, the
state directory or git hooks. Those are lane 3 for a reason.

## Writing a good brief

- Self-contained: the advisor sees only the brief and read-only files.
- One exact question, stated first.
- Two or three concrete options, with the trade-off of each.
- The relevant diff or code excerpt, not the whole file.
- No secrets, tokens or credentials.
- Under about 100 lines.

## Judgement that stays with you

The rules catch commands by their text. They cannot see an architectural or
product fork, such as choosing a data model, a public API shape or a new
service boundary. Deciding that such a fork deserves a consult is your job.
When you meet one, pick a stable decision id and a seat (`architecture`,
`ux`, `code` or `tiebreak`) and run `babysitter consult` yourself.

## Before you report work as done

Run `babysitter checkpoint`. If it returns 10, consult on the change set as
above before you report.

## Logging

After each consult, log one line in your progress notes:

    Advisor: <seat> | Verdict: <summary> | Action: <what you did>
