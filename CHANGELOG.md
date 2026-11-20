# Changelog

All notable changes to this project are recorded here. Versions follow
[Semantic Versioning](https://semver.org/). While the version is below 1.0,
any release may change behaviour.

## 0.2.0 (alpha)

Renamed from agent-babysitter to Agentsitter. No behaviour changes beyond the
names. See "Upgrading from agent-babysitter 0.1.0" in `README.md`.

- Breaking: the repository, the distribution and the Python package are now
  `agentsitter`; the `babysitter` and `agent-babysitter` commands are now one
  command, `agentsitter`.
- Breaking: the default folders are now `~/.config/agentsitter` and
  `~/.local/state/agentsitter`, the default signing namespace is `agentsitter`,
  and the `BABYSITTER_*` environment variables are now `AGENTSITTER_*`.
- `agentsitter install-hooks` replaces hooks written by 0.1.0 without `--force`.
- The lane-3 self-protection rules and the agent hook still guard the 0.1.0
  command names, variables and folders, so an old copy installed alongside this
  one cannot be used to approve or to change the governance files.
- The clean-room test no longer reads the `.git` file of a git worktree.

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
