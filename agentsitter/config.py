"""Configuration: built-in defaults merged with one user-level JSON file.

There is deliberately no per-repository config file. A file inside the working
tree is something the governed agent edits all day, so the rules, the budget
and the trust anchor live outside it.
"""

import copy
import json
import os
import re
from pathlib import Path

FAMILIES = ("claude", "gemini", "codex", "grok")
SEATS = ("architecture", "ux", "code", "tiebreak")
SYSTEM_TEMP = "<system-temp>"

DEFAULTS = {
    # Who signs approvals. `identity` is the principal in the allowed-signers file.
    "approver": {
        "name": "the approver",
        "identity": "approver",
        "allowed_signers": None,
        "signing_key": None,
    },
    "approval": {"namespace": "agentsitter", "ttl_seconds": 900},
    # Model family driving the session. Normally set per session through
    # AGENTSITTER_HOST_FAMILY; unknown means every consult fails closed.
    "host_family": None,
    # Environment variables that prove which family is running, when present.
    "family_markers": {"CLAUDECODE": "claude", "GEMINI_CLI": "gemini"},
    "budget": {"per_decision": 2, "per_window": 6},
    # `model: null` means "use the CLI's own default model".
    "advisors": {
        "claude": {"model": None, "timeout": 180},
        "gemini": {"model": None, "timeout": 360},
        "codex": {"model": None, "timeout": 600},
        "grok": {"model": None, "timeout": 900},
    },
    # seats[builder family][seat] -> which advisor family answers.
    # A seat may also carry its own "model" and "timeout".
    "seats": {
        "claude": {
            "architecture": {"advisor": "gemini"},
            "ux": {"advisor": "gemini"},
            "code": {"advisor": "codex"},
            "tiebreak": {"advisor": "grok"},
        },
        "gemini": {
            "architecture": {"advisor": "claude"},
            "ux": {"advisor": "claude"},
            "code": {"advisor": "codex"},
            "tiebreak": {"advisor": "grok"},
        },
        "codex": {
            "architecture": {"advisor": "claude"},
            "ux": {"advisor": "gemini"},
            "code": {"advisor": "grok"},
            "tiebreak": {"advisor": "gemini"},
        },
        "grok": {
            "architecture": {"advisor": "claude"},
            "ux": {"advisor": "gemini"},
            "code": {"advisor": "codex"},
            "tiebreak": {"advisor": "gemini"},
        },
    },
    # Directory names that count as disposable anywhere below the project root.
    "scratch_dirs": [
        "scratch", "tmp", ".tmp", "temp", "node_modules", "dist", "build", "out",
        "target", "coverage", "__pycache__", ".pytest_cache", ".mypy_cache",
        ".ruff_cache", ".next", ".cache",
    ],
    # Absolute directories whose contents are disposable.
    "scratch_roots": [SYSTEM_TEMP],
    # When a finished change set is big or touches sensitive paths, it is lane 2.
    "changeset": {
        "max_files": 8,
        "max_lines": 300,
        "sensitive_paths": [
            r"(^|/)migrations?/", r"(^|/)schema\.", r"(^|/)\.github/workflows/",
            r"(^|/)Dockerfile", r"(^|/)docker-compose", r"\.tf$",
            r"(^|/)(auth|security|crypto|payments?|billing)(/|\.|_)",
            r"(^|/)(package\.json|pyproject\.toml|requirements[^/]*\.txt|Cargo\.toml|go\.mod|Gemfile|composer\.json)$",
        ],
    },
    "rules": {"extra": [], "disable": []},
    # Regular expressions for paths (with forward slashes) that are lane 3 to edit
    # or to name in a shell command, on top of the config, the trust anchor, the
    # state, git hooks and this package. The default is the harness settings file
    # that installs the agent hook; add your own harness's file if it differs.
    "protected_paths": [r"\.claude/settings(?:\.local)?\.json"],
    # Path to a POSIX bash for the advisor wrappers (found automatically if null).
    "bash": None,
}


class ConfigError(Exception):
    """The configuration is unusable. Callers fail closed on this."""


def defaults():
    return copy.deepcopy(DEFAULTS)


def merge(base, override):
    """Deep-merge two dicts into a new one. Lists and scalars are replaced."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def home(env=None):
    env = os.environ if env is None else env
    return Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())


def config_path(env=None):
    env = os.environ if env is None else env
    explicit = env.get("AGENTSITTER_CONFIG")
    return Path(explicit) if explicit else home(env) / ".config" / "agentsitter" / "config.json"


def state_root(env=None):
    env = os.environ if env is None else env
    explicit = env.get("AGENTSITTER_STATE_DIR")
    return Path(explicit) if explicit else home(env) / ".local" / "state" / "agentsitter"


def allowed_signers_path(cfg, env=None):
    explicit = cfg["approver"].get("allowed_signers")
    if explicit:
        return Path(explicit).expanduser()
    return home(env) / ".config" / "agentsitter" / "allowed_signers"


def validate(cfg):
    unknown = set(cfg) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")
    if cfg["host_family"] not in (None, *FAMILIES):
        raise ConfigError(f"host_family must be one of {FAMILIES} or null")
    for marker, family in cfg["family_markers"].items():
        if family not in FAMILIES:
            raise ConfigError(f"family_markers[{marker}] names unknown family {family!r}")
    for key in ("per_decision", "per_window"):
        if not isinstance(cfg["budget"].get(key), int) or cfg["budget"][key] < 1:
            raise ConfigError(f"budget.{key} must be a positive integer")
    if not isinstance(cfg["approval"].get("ttl_seconds"), int) or cfg["approval"]["ttl_seconds"] < 1:
        raise ConfigError("approval.ttl_seconds must be a positive integer")
    for builder, seats in cfg["seats"].items():
        if builder not in FAMILIES:
            raise ConfigError(f"seats names unknown builder family {builder!r}")
        for seat, entry in seats.items():
            if seat not in SEATS:
                raise ConfigError(f"seats.{builder} names unknown seat {seat!r}")
            advisor = entry.get("advisor")
            if advisor not in FAMILIES:
                raise ConfigError(f"seats.{builder}.{seat} names unknown advisor {advisor!r}")
            if advisor == builder:
                raise ConfigError(f"seats.{builder}.{seat} routes a builder to its own family")
    if not isinstance(cfg["protected_paths"], list):
        raise ConfigError("protected_paths must be a list of regular expressions")
    for pattern in cfg["protected_paths"]:
        try:
            re.compile(pattern)
        except (re.error, TypeError) as exc:
            raise ConfigError(f"protected_paths: bad pattern {pattern!r}: {exc}") from exc
    for rule in cfg["rules"]["extra"]:
        if not isinstance(rule, dict) or not rule.get("id") or rule.get("lane") not in (2, 3):
            raise ConfigError("each extra rule needs an id and a lane of 2 or 3")
        for field in ("pattern", "unless"):
            if rule.get(field):
                try:
                    re.compile(rule[field])
                except re.error as exc:
                    raise ConfigError(f"rule {rule['id']}: bad {field}: {exc}") from exc
        if not rule.get("pattern"):
            raise ConfigError(f"rule {rule['id']}: pattern is required")
    return cfg


def load(env=None):
    """Defaults, overlaid with the user config file when it exists."""
    env = os.environ if env is None else env
    cfg = defaults()
    path = config_path(env)
    if path.is_file():
        try:
            override = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        if not isinstance(override, dict):
            raise ConfigError(f"{path} must hold a JSON object")
        cfg = merge(cfg, override)
    elif env.get("AGENTSITTER_CONFIG"):
        raise ConfigError(f"AGENTSITTER_CONFIG points at a missing file: {path}")
    try:
        return validate(cfg)
    except (AttributeError, KeyError, TypeError) as exc:
        # A section of the wrong type (null, a list, a string) surfaces here.
        raise ConfigError(f"{path} has a section of the wrong shape: {exc}") from exc
