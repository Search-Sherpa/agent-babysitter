"""On-disk state for one governed project: approvals, consult receipts, ledgers.

State lives outside the working tree, keyed by the project's path, so cleaning
or resetting the repository does not erase it.
"""

import hashlib
import json
import os
import time
from pathlib import Path

from . import config


class State:
    def __init__(self, root):
        self.root = Path(root)

    def _dir(self, name):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def approvals(self):
        return self._dir("approvals")

    @property
    def spent(self):
        return self._dir("spent")

    @property
    def receipts(self):
        return self._dir("receipts")

    def append(self, ledger, record):
        """Append one JSON record to a ledger file."""
        self.root.mkdir(parents=True, exist_ok=True)
        record = {"at": int(time.time()), **record}
        with open(self.root / f"{ledger}.jsonl", "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    def records(self, ledger):
        path = self.root / f"{ledger}.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def project_key(project_root):
    normal = os.path.normcase(os.path.realpath(str(project_root)))
    return hashlib.sha256(normal.encode("utf-8")).hexdigest()[:16]


def for_project(project_root, env=None):
    return State(config.state_root(env) / project_key(project_root))
