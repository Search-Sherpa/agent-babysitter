# Agentsitter

> **Alpha (0.2.0).** This is an early release. The automated tests pass on
> Linux, macOS and Windows in CI. A real approval at a terminal, with a
> passphrase-protected key, has been checked by hand on Windows 11 only, with
> release 0.1.0 (named agent-babysitter; 0.2.0 renames it). It
> reduces the chance that an agent takes an irreversible step unnoticed; it
> is not a sandbox. Read [what it does not enforce](#what-this-does-and-does-not-enforce) first.

Agentsitter puts three lanes of governance around an autonomous coding
agent. Routine commands run without fuss. Non-trivial commands, such as adding
a dependency, need one consult with an advisor CLI from a different model
family on record first. Irreversible commands, such as a commit, a push or a
publish, need a signed, single-use approval from a human, bound to that
action (a table below says to what, for each kind of action) and checked with
`ssh-keygen -Y verify`. It is a small, standard-library
Python package plus four shell wrappers for advisor CLIs. It is meant to let an
agent keep working on its own while the decisions that matter still reach a
second model or a person.

## The three lanes

| Lane | What triggers it | What unlocks it | How it is enforced |
|---|---|---|---|
| 1, routine | Anything no rule matches: reading, editing source, tests, `git status`, deleting scratch output | Nothing; it runs | Nothing to enforce |
| 2, non-trivial | Dependency changes (`npm install <pkg>`, `pip install <pkg>`, `cargo add`, ...), migration authoring (`alembic revision`, `prisma migrate dev`, ...), and a finished change set that is large or touches sensitive paths | One consult verdict on record for that exact decision, from an advisor of another model family (or two failed consult attempts, after which the agent proceeds on its own judgment and that is logged) | `agentsitter run` / `check`, the agent-harness hook, and the harness `Stop` hook for change sets |
| 3, irreversible | Commits, pushes, merges, rebases, history rewrites, publishes, deploys, infrastructure applies, HTTP writes to non-local hosts, mail, secret-store writes, `.env` writes, privilege elevation, deletion outside scratch space, and anything that touches the governance system itself | A signed approval for that exact action, made by the approver with `agentsitter approve` in their own terminal; each approval works once and expires (15 minutes by default) | Git hooks (commit, merge commit, push, rebase), `agentsitter run` / `check`, and the agent-harness hook (shell commands and edits to protected files) |

The highest lane found in a command wins. `npm install left-pad && git push` is lane 3.

## Requirements

- Python 3.9 or newer. No third-party packages.
- git.
- A POSIX bash for the advisor wrappers. On Windows, the bash that ships with
  Git for Windows is found automatically.
- OpenSSH `ssh-keygen` with `-Y sign` and `-Y verify` support (OpenSSH 8.1 or newer).
- At least one advisor CLI from a model family other than the one driving
  your agent. Supported advisors: `claude`, a Gemini CLI (`agy` by default),
  `codex` and `grok`.

## Install

```sh
pip install -e .
```

This installs the `agentsitter` command. Without installing, run
`python -m agentsitter <command>` from the repository root.

### Upgrading from agent-babysitter 0.1.0

Release 0.1.0 was called agent-babysitter. Release 0.2.0 renames everything
and changes nothing else. Do these steps in your own terminal, in this order.

1. Install this release next to the old one: `pip install .`
2. Move your key, config and state to the new folders:

   ```sh
   mv ~/.config/agent-babysitter ~/.config/agentsitter
   mv ~/.local/state/agent-babysitter ~/.local/state/agentsitter
   ```

3. Rename the variables you set: `BABYSITTER_HOST_FAMILY`, `BABYSITTER_CONFIG`,
   `BABYSITTER_STATE_DIR` and `BABYSITTER_BASH` become `AGENTSITTER_HOST_FAMILY`,
   `AGENTSITTER_CONFIG`, `AGENTSITTER_STATE_DIR` and `AGENTSITTER_BASH`. The old
   names are ignored.
4. In your agent harness settings, change the command `babysitter hook agent`
   to `agentsitter hook agent`. Do this before step 7: a harness may let a tool
   call through when its hook command cannot run (Claude Code blocks only on
   exit 2).
5. Run `agentsitter install-hooks` in each governed repository. It replaces the
   0.1.0 hooks without `--force`.
6. Replace the skill you gave the agent with the new `skill/SKILL.md`.
7. Remove the old release: `pip uninstall agent-babysitter`. Then check
   everything with `agentsitter status`.

The default signing namespace is now `agentsitter`. A `config.json` copied from
the 0.1.0 example sets `"agent-babysitter"` explicitly, which keeps working as
long as your allowed-signers line names the same namespace. To switch, change
both. An approval requested under 0.1.0 does not carry over; request it again.

## Setup

Do these steps yourself, in your own terminal, not through the agent. Several
of them are lane 3 by design when an agent tries them.

1. Create a signing key with a passphrase (you are prompted for it):

   ```sh
   mkdir -p ~/.config/agentsitter
   ssh-keygen -t ed25519 -C approver -f ~/.config/agentsitter/approver_ed25519
   ```

   Do not leave the passphrase empty. A hardware-backed key (`-t ed25519-sk`) also works.

2. Write the allowed-signers file. The principal must match
   `approver.identity` and the namespace must match `approval.namespace`:

   ```sh
   printf 'approver namespaces="agentsitter" %s\n' \
     "$(cat ~/.config/agentsitter/approver_ed25519.pub)" \
     > ~/.config/agentsitter/allowed_signers
   ```

3. Copy the example config and edit it:

   ```sh
   cp agentsitter.config.example.json ~/.config/agentsitter/config.json
   ```

   Replace the `gemini-REPLACE-...` placeholders with a real Gemini model id
   if you use the Gemini advisor, or remove those entries if you do not.

4. Tell each agent session which model family drives it, for example in the
   shell that launches the agent:

   ```sh
   export AGENTSITTER_HOST_FAMILY=claude   # or gemini, codex, grok
   ```

   If this is unset and no session marker identifies the family, every
   consult fails closed with exit 14.

5. Install the git hooks in each repository you want governed:

   ```sh
   cd path/to/repo
   agentsitter install-hooks
   ```

   This refuses to overwrite hooks it did not write unless you pass
   `--force`. The hooks record the Python interpreter and package location at
   install time, so run it again if either moves. A hook counts as installed
   only when it is exactly the hook this installation writes; `agentsitter
   status` lists any hook of ours that needs reinstalling. The hook runs
   Python with `-I -S` and loads this package from the recorded location only,
   so neither a file in the work tree, a `PYTHON*` variable nor a start-up file
   in site-packages can stand in for the standard library or for this package.

6. Add the agent hook to your agent harness. Any harness that speaks the
   JSON-on-stdin hook protocol works (exit 0 allows, exit 2 blocks and shows
   stderr to the agent); Claude Code is one. Merge
   `examples/agent-hook-settings.json` into the harness settings. It runs
   `agentsitter hook agent` before every `Bash`, `PowerShell`, `Write`, `Edit`,
   `MultiEdit` and `NotebookEdit` tool call, and when the agent stops.

7. Give the agent `skill/SKILL.md`, which tells it how to behave under these lanes.

Check the result with `agentsitter status`.

## The approval flow, worked through

The agent tries to publish a package. With the harness hook installed, the
tool call is blocked and the agent sees:

```text
agentsitter: `npm publish` is lane 3 and needs a signed human approval. Request ccff4ed366e2208e is open. Ask the approver to run `agentsitter approve ccff4ed366e2208e` in their own terminal, then retry the identical action. An advisor's opinion never substitutes for this approval.
```

Without the harness hook, `agentsitter run -- npm publish` prints the same
message and exits 20. The agent stops and gives you a readiness summary with
the request id. You look at what is pending:

```text
$ agentsitter requests
ccff4ed366e2208e  command  npm publish  (in /work/myapp)
```

You approve it in your own terminal:

```text
$ agentsitter approve ccff4ed366e2208e
Request ccff4ed366e2208e
  command: "npm publish"
  cwd: "/work/myapp"
  kind: "command"
Sign this approval? [y/N] y
```

`ssh-keygen` then asks for your key's passphrase on your terminal (it is
handed the terminal directly, and prints its own messages there), and the command prints
`Signed approval for request ccff4ed366e2208e.` The agent retries the identical
command and it runs. A second `npm publish` needs a new approval.

`approve` refuses to sign (exit 22) when stdin is not a terminal, when
`ADVISOR_DEPTH` is set, or when a session marker such as `CLAUDECODE` is set.
Run it in a plain terminal, not inside an agent session.

Commits work the same way through the git hook. A blocked commit prints:

```text
agentsitter: blocked git commit (git-commit) in /work/myapp.
git commit (git-commit) is lane 3 and needs a signed human approval. Request 8fd20687fb44498d is open. Ask the approver to run `agentsitter approve 8fd20687fb44498d` in their own terminal, then retry the identical action. An advisor's opinion never substitutes for this approval.
Retry the same git command after approving. A human can bypass their own hooks with git's --no-verify.
```

What an approval is bound to depends on the kind of action:

| Action | The approval covers |
|---|---|
| Commit | The repository, the staged tree, the parent commit, and the other parents when a merge is being concluded |
| Push | The repository, the remote and the exact ref updates |
| Rebase | The repository, the upstream and the branch (by name and by the commit each names now) and the current HEAD. A rebase with any option (`--onto`, `-i`, ...) is approved as a command as well, because git tells the hook only the upstream and the branch |
| Edit of a protected file | The tool, the path and the full edit. `agentsitter approve` prints the edit itself, and refuses to sign if the request no longer carries it |
| Any other command | The command text and the directory it runs in, nothing more. `npm publish` approved in a directory publishes whatever that directory holds when it runs, within the approval's lifetime |

Staging different content after a commit approval needs a new approval.

A blocked merge commit leaves the merge staged, and git refuses a second
`git merge` until it is concluded. After approving the `git-merge-commit`
request, run `git merge --abort` and then the identical `git merge` again.
Concluding with `git commit` (or `git merge --continue`) instead runs the
`pre-commit` hook, which opens a separate `git-commit` request that also needs
an approval.

For lane 2 the agent sees, for example:

```text
`npm install left-pad` is lane 2 and needs one cross-family consult on record first. Write the question and its context to a brief file, then run: agentsitter consult --decision sha256:3d2f... --seat architecture --brief-file <file> and retry once you have weighed the verdict.
```

## Command reference

| Command | What it does |
|---|---|
| `agentsitter consult --decision ID --seat SEAT --brief-file FILE [--advisor FAMILY] [--cwd DIR]` | Run one consult. The seat (`architecture`, `ux`, `code`, `tiebreak`) picks the advisor family from the routing table; `--advisor` overrides it but can never name the builder's own family. The advisor may read `--cwd` (default: the current directory). A reply with a `VERDICT:` line is put on record. |
| `agentsitter classify -- CMD...` | Print the lane of a command and the rules it matched. Changes nothing. |
| `agentsitter check [--json] -- CMD...` | Say whether a command may run now and what to do if not. Opens an approval request for lane 3. Does not use up an approval. |
| `agentsitter run -- CMD...` | Run the command with bash only if its lane allows it now. Uses up the approval it relies on, after checking that bash starts, so a broken bash path does not cost an approval. Exits with the command's own code when it runs. One argument after `--` is a shell command line, taken as written (`agentsitter run -- "make build && make test"`). Several arguments are separate words and stay separate, except a bare operator such as `&&`. The same holds for `classify` and `check`. |
| `agentsitter checkpoint [--json]` | Check the uncommitted change set of the current repository, including untracked files. Lane 2 when it is large or touches sensitive paths. |
| `agentsitter requests [--json]` | List pending approval requests. |
| `agentsitter approve REQUEST_ID [--key PATH]` | Approver only. Show the request and sign it after a `y`. The key defaults to `approver.signing_key`. |
| `agentsitter budget reset` | Reset the consult budget window. This is itself lane 3 and needs an approval. |
| `agentsitter install-hooks [--repo DIR] [--force]` | Install the `pre-commit`, `pre-merge-commit`, `pre-push` and `pre-rebase` hooks. |
| `agentsitter status` | Show the builder family, consults used, installed hooks, the trust anchor and pending requests. |
| `agentsitter hook agent` | Entry point for the agent harness. Reads the hook JSON on stdin. Any internal error blocks the tool call. |
| `agentsitter hook git NAME ...` | Entry point for the installed git hooks. |

When the agent hook is active, a plain `git commit`, `git push` or
`git rebase` is left to the installed git hook, which binds the approval to
the exact content. This deferral applies only when all of these hold:

- every command on the line is git itself (`git add -A && git commit` is
  fine), not a string handed to `bash -c`, `eval` or `ssh`, and not behind a
  wrapper other than `time` or `rtk`. Anything else on the line can change
  what `git` means, for example a shell function of that name;
- the commit is not an amend and carries no `--no-verify` or `-n`, in any
  spelling git accepts;
- no option comes before the subcommand (`-C`, `-c`, `--git-dir`,
  `--work-tree`, `--config-env` and the rest);
- the command line changes no directory and sets or removes no variable at
  all. The hook finds its config, trust anchor and state through `HOME`, and
  its tools through `PATH`, so `HOME=... git commit` is not left to the hook;
- a rebase carries no option;
- the hook git would run is exactly the one this installation writes.

Otherwise the command itself needs an approval, and the git hook asks for its
own on top.

Inside a session governed by the harness hook, do not wrap commands in
`agentsitter run --`. A wrapped lane 3 command is blocked with an instruction
to run it directly, and no request is opened for the wrapped form.

## Exit codes

Codes from `agentsitter` commands (`agentsitter/codes.py`):

| Code | Meaning |
|---|---|
| 0 | Done; the command may proceed |
| 2 | Bad arguments, unusable config or a missing tool |
| 4 | Called from inside an advisor; consults do not nest |
| 10 | Lane 2: a consult must be on record first |
| 11 | The advisor failed or gave no verdict |
| 12 | A consult budget cap was hit; the approver must reset it |
| 13 | The advisor is the builder's own model family |
| 14 | The builder's model family cannot be established |
| 20 | Lane 3: a signed human approval is required |
| 21 | The approval on file is not valid (expired, badly signed, or for another action) |
| 22 | The approve command refused to sign |

The agent-harness hook itself exits 0 (allow) or 2 (block). A git hook exits
with the agentsitter code, and git reports the hook as failed.

Codes from the advisor wrappers, when you run them directly. `agentsitter
consult` reports any wrapper failure as 11 and shows the wrapper's stderr.

| Code | Meaning |
|---|---|
| 1 | Codex wrapper only: the run failed or the answer was empty |
| 2 | Usage error or an invalid tunable; the wrapper refused to run |
| 3 | The CLI exited 0 but printed no answer; treated as no review |
| 4 | Recursive advisor call blocked (`ADVISOR_DEPTH` is 1 or more) |
| 5 | Grok wrapper only: no Grok login, or it expires before the timeout ends |
| 6 | The builder family is unknown (`AGENTSITTER_HOST_FAMILY` unset or not recognised) |
| 7 | The builder is the advisor's own family; a same-family consult is blocked |
| 124 | Timed out; no review was produced |

## Configuration reference

The config is one JSON file, `~/.config/agentsitter/config.json` (or the
path in `AGENTSITTER_CONFIG`), deep-merged over the built-in defaults. There is
deliberately no per-repository config file, because the agent edits the
working tree all day. Unknown top-level keys are an error.

| Key | Default | Meaning |
|---|---|---|
| `approver.name` | `"the approver"` | A display label. The code does not currently use it. |
| `approver.identity` | `"approver"` | The principal checked in the allowed-signers file. |
| `approver.allowed_signers` | `null` | Path to the allowed-signers file (the trust anchor). `null` means `~/.config/agentsitter/allowed_signers`. |
| `approver.signing_key` | `null` | Default private key for `agentsitter approve`. |
| `approval.namespace` | `"agentsitter"` | The `ssh-keygen -Y` namespace. Must match `namespaces=` in the allowed-signers line. |
| `approval.ttl_seconds` | `900` | How long a signed approval stays valid. |
| `host_family` | `null` | The builder family when `AGENTSITTER_HOST_FAMILY` is not set. |
| `family_markers` | `{"CLAUDECODE": "claude", "GEMINI_CLI": "gemini"}` | Environment variables that identify the running family. A declared family that contradicts a marker fails closed. |
| `budget.per_decision` | `2` | Consult attempts allowed per decision. |
| `budget.per_window` | `6` | Consults allowed until the approver resets the budget. |
| `advisors.<family>.model` | `null` | Model passed to that advisor's CLI. `null` uses the CLI's default, except that the Gemini wrapper requires a `gemini-*` model. |
| `advisors.<family>.timeout` | 180, 360, 600, 900 | Seconds for claude, gemini, codex and grok. |
| `seats.<builder>.<seat>.advisor` | see `config.py` | Which family answers each seat for each builder. Routing a builder to its own family is an error. |
| `seats.<builder>.<seat>.model`, `.timeout` | unset | Optional per-seat overrides of the advisor's model and timeout. |
| `scratch_dirs` | `scratch`, `tmp`, `build`, `dist`, `node_modules`, ... | Directory names whose contents are disposable anywhere below the project root. |
| `scratch_roots` | the system temp directory | Absolute directories whose contents are disposable. |
| `changeset.max_files` | `8` | A finished change set with more files is lane 2. |
| `changeset.max_lines` | `300` | A finished change set with more changed lines is lane 2. |
| `changeset.sensitive_paths` | migrations, schema, CI workflows, Docker, Terraform, auth and payments code, dependency manifests | Regular expressions; a change set touching a match is lane 2. |
| `rules.extra` | `[]` | Your own rules. Each needs an `id`, a `lane` of 2 or 3 and a regular expression `pattern`. Optional: `unless` (a pattern that cancels the match), `category`, and `seat` for lane 2. |
| `rules.disable` | `[]` | Rule ids to turn off, including built-in ones and `delete-outside-scratch`, `protected-path` and `governance-code`. |
| `protected_paths` | the Claude Code settings files (`.claude/settings.json`, `.claude/settings.local.json`) | Regular expressions, matched against paths with forward slashes. A match is lane 3 to edit or to name in a shell command. Add the settings file of your own harness, since removing the hook from it turns the harness hook off. The config, the trust anchor, the state directory, git hooks and this package's own files are always protected. |
| `bash` | `null` | Path to a POSIX bash for the wrappers when `AGENTSITTER_BASH` is not set. |

Environment variables are listed in `.env.example`.

## What the advisor wrappers lock down

Each wrapper in `agentsitter/advisors/` runs its CLI non-interactively
and read-only, and all of them:

- refuse to run inside another advisor (`ADVISOR_DEPTH`), so consults never nest;
- refuse when the builder family is unknown or is the advisor's own family;
- close stdin, enforce a wall-clock timeout, and treat empty output as a failure, never as a clean review.

Per CLI:

- Claude: `--permission-mode plan`, `--restricted`, `--no-session-persistence`, `--strict-mcp-config`.
- Gemini: `--sandbox --mode plan`, and an explicit `gemini-*` model is required, because the host CLI can serve other model families.
- Codex: `--sandbox read-only`, an ephemeral run, the user config ignored (which keeps configured MCP servers and plugins out), web search off, and connectors, plugins, browser, computer-use and image tools disabled by name.
- Grok: only the `read_file`, `grep` and `list_dir` tools, `--sandbox strict`, no subagents, no web search, MCP tools denied, and a check that the login will not expire mid-call.

## What this does and does not enforce

Read this before you rely on it.

- It is a tripwire, not a sandbox. The agent runs with your own user
  permissions. Agentsitter stops the commands it recognises at the
  points where it is wired in. It cannot stop a process that goes around it.
  Without the harness hook, `agentsitter run` and `agentsitter check` are
  opt-in: only the git hooks act on an agent that never calls them.
- The signing key must be passphrase-protected or hardware-backed. Reading
  files is not gated. An unencrypted private key that the agent can read lets
  the agent sign its own approvals.
- Git hooks can be skipped. `--no-verify` skips them, and the agent hook
  treats it as lane 3, but an agent running without the harness hook can use
  it freely. Protect important branches on the server as well.
- Classification is pattern matching on command text. It can be evaded by
  indirection, for example a script or a Makefile target that runs the gated
  command. Add `rules.extra` entries for the indirections you know about.
  Known gaps in what the built-in rules catch: `git rm` without `--force` is
  routine (git can restore the file), a glob that reaches outside scratch
  space through a symbolic link is judged by its fixed prefix, an HTTP
  client that posts without being told to (HTTPie with request items) is
  missed, a write to this machine is waived even when a second destination
  is written without `http://` or the request is steered elsewhere with
  `--connect-to`, and a shell redirection into a link that points at a
  protected file is judged by the link's own name. Two independent reviews
  each found further spellings the rules missed; expect more.
- The Python interpreter and its standard library are not protected. They
  are ordinary files the agent can write when they sit in your home
  directory or in a virtual environment, and the hooks run them.
- Lane 2 is advice, not a gate. The agent writes the brief, so it shapes the
  verdict; two failed attempts let it proceed on its own judgment; and the
  budget is counted after each consult, so concurrent consults can overrun it.
  Nothing binds the advisor's answer to the decision beyond the id the agent
  passes.
- A commit approval can be raced. The hook reads the staged tree, checks the
  approval, and git then builds the commit from the index. A second process
  that restages between those two moments changes what is committed.
- The harness hook protects this package's own files and the harness settings
  named in `protected_paths`. If you install with `pip install -e .` and then
  govern work on that same checkout, every edit to the package needs an
  approval; install a separate copy to govern its own development.
- A consult for an architectural or product fork that no rule detects still
  depends on the agent deciding to ask.
- Config and state live in your home directory (`~/.config/agentsitter`
  and `~/.local/state/agentsitter`). The harness hook gates the agent's
  edits and commands that touch them, but an agent without the harness hook
  could alter them.
- The terminal check and the real passphrase prompt in `agentsitter approve`
  cannot be exercised by the automated tests. They were checked by hand once,
  on Windows 11 with OpenSSH for Windows 9.5 in PowerShell: the prompt was
  visible, the approval was signed, the command ran once, and the next run
  was refused. No other platform has had that check.
- The advisor wrappers depend on each vendor CLI's current flags. A CLI
  release that renames or drops a flag can break a wrapper or weaken its
  lockdown.

  One live call per wrapper was made on 2026-11-19 on Windows 11, each asking
  for a one-word reply:

  | Wrapper | CLI version | Result |
  |---|---|---|
  | `ask-claude.sh` | Claude Code 2.1.319 | Answered, exit 0 |
  | `ask-codex.sh` | codex-cli 0.162.1 | Answered, exit 0 |
  | `ask-gemini.sh` | agy 1.5.13 | Answered, exit 0 |
  | `ask-grok.sh` | grok 1.0.77 | Not exercised: the wrapper's login check stopped before the call (exit 5, login expired) |

  This shows the flags were accepted on those versions. It does not test the
  lockdown each wrapper asks the CLI for.

## Running the tests

```sh
python -m unittest discover -s tests -t .
```

The suite needs git, bash and `ssh-keygen`. It uses stub advisor CLIs from
`tests/stubs/`, throwaway keys and temporary repositories, and never calls a
real model.

`tests/test_clean_room.py` scans every file in the repository for absolute
machine paths, email addresses outside `example.invalid`, pinned model ids and
CR line endings. To also ban your own terms (names, hostnames) without putting
the list in the repository, supply them from outside:

```sh
AGENTSITTER_LINT_BANNED="first term,second term" python -m unittest tests.test_clean_room
AGENTSITTER_LINT_BANNED_FILE=/path/to/terms.txt python -m unittest tests.test_clean_room
```

The file holds one term per line. Matching ignores case. A file named
`.lint-banned.local` at the repository root is ignored by git and skipped by
the scan, so you can keep a local list there.

## Licence

MIT. See `LICENSE`.
