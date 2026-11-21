# Security policy

Agentsitter is alpha software. Read "What this does and does not enforce" in
`README.md` before relying on it: it lists what the tool does not protect
against.

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Please report privately through GitHub: open the repository's **Security**
tab and choose **Report a vulnerability**. Do not open a public issue for a
problem that lets an agent get past a lane.

A useful report says:

- the version or commit you tested,
- your operating system, Python version and shell,
- the exact command or tool call that got through, and
- what you expected the tool to do instead.

## What counts

In scope:

- a command or edit that should be lane 2 or lane 3 and runs without the
  consult or approval it needs, while the documented enforcement points are
  installed;
- an approval that works twice, works after it expires, or works for an
  action other than the one that was signed;
- a way for the governed agent to sign or forge an approval;
- an advisor wrapper that lets the advisor CLI write files or run commands.

Already documented and not a new report: the limits listed in that section of
`README.md`, such as commands the classifier does not recognise, and an agent
that runs without the harness hook.
