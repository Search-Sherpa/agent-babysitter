# Changelog

All notable changes to this project are recorded here. Versions follow
[Semantic Versioning](https://semver.org/). While the version is below 1.0,
any release may change behaviour.

## 0.1.0 (alpha)

First public release.

- Three lanes for agent commands: routine work runs, non-trivial work needs
  one consult with an advisor from another model family, irreversible work
  needs a signed human approval.
- Approvals are signed with `ssh-keygen -Y sign`, checked with
  `ssh-keygen -Y verify`, bound to one action, single use, and expire.
- Enforcement points: git hooks (`pre-commit`, `pre-merge-commit`, `pre-push`,
  `pre-rebase`), the `babysitter run` and `babysitter check` commands, and a
  hook for agent harnesses that speak the JSON-on-stdin hook protocol.
- Read-only advisor wrappers for the `claude`, `codex`, `grok` and Gemini
  (`agy`) CLIs, with a consult budget.
- Standard library only. Python 3.9 or newer.

Known limits are listed in `README.md`.
