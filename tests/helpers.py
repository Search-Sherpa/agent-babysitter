"""Shared test fixtures: throwaway signing keys, git repos, stub advisor CLIs, CLI runs."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STUBS = REPO_ROOT / "tests" / "stubs"
NAMESPACE = "agentsitter"


def need(tool):
    path = shutil.which(tool)
    if not path:
        raise RuntimeError(f"{tool} is required to run this test suite")
    return path


def make_key(directory, name):
    """Create an unencrypted ed25519 key pair. Returns (private, public) paths."""
    private = Path(directory) / name
    subprocess.run(
        [need("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(private)],
        check=True, capture_output=True,
    )
    return private, Path(str(private) + ".pub")


def trust(directory, public_key, identity="approver"):
    """Write an allowed-signers file trusting one public key."""
    path = Path(directory) / "allowed_signers"
    key = Path(public_key).read_text(encoding="utf-8").strip()
    # open() rather than Path.write_text: its newline argument needs Python 3.10.
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f'{identity} namespaces="{NAMESPACE}" {key}\n')
    return path


def git(repo, *args, env=None, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=env, check=check)


def make_repo(directory):
    """An initialised repo with one commit and a local identity."""
    repo = Path(directory)
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test Builder")
    git(repo, "config", "user.email", "builder@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "seed")
    return repo


def base_env(tmp, config=None, family="claude", stub_mode="verdict"):
    """An isolated environment: own state dir, own config, stub advisors first on PATH."""
    tmp = Path(tmp)
    env = dict(os.environ)
    for name in list(env):
        if name.startswith("AGENTSITTER_") or name in ("ADVISOR_DEPTH", "CLAUDECODE", "GEMINI_CLI"):
            del env[name]
    config_file = tmp / "config.json"
    config_file.write_text(json.dumps(config or {}), encoding="utf-8")
    # The grok wrapper checks a login file before it runs; give it a live one.
    grok_login = tmp / "grok-auth.json"
    grok_login.write_text('{"expires_at": "2099-01-01T00:00:00Z"}\n', encoding="utf-8")
    env.update({
        "ASK_GROK_AUTH_FILE": str(grok_login),
        "AGENTSITTER_CONFIG": str(config_file),
        "AGENTSITTER_STATE_DIR": str(tmp / "state"),
        "PYTHONPATH": str(REPO_ROOT),
        "PATH": str(STUBS) + os.pathsep + env["PATH"],
        "STUB_MODE": stub_mode,
        "STUB_LOG": str(tmp / "stub-calls.log"),
    })
    if family:
        env["AGENTSITTER_HOST_FAMILY"] = family
    return env


def cli(args, env, cwd, stdin=None):
    """Run the agentsitter CLI as a subprocess. Returns CompletedProcess."""
    return subprocess.run([sys.executable, "-m", "agentsitter", *args],
                          capture_output=True, text=True, env=env, cwd=str(cwd), input=stdin)


def cli_bytes(args, env, cwd):
    """Like cli, but stdout and stderr come back as raw bytes, undecoded."""
    return subprocess.run([sys.executable, "-m", "agentsitter", *args],
                          capture_output=True, env=env, cwd=str(cwd), stdin=subprocess.DEVNULL)
