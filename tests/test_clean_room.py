"""Clean-room scan: the repository must not leak machine paths, addresses, pinned models or CRLF.

Extra banned terms (names, hostnames and so on) are supplied from outside the
repository, so the list itself never lands in it:

  BABYSITTER_LINT_BANNED        comma-separated terms
  BABYSITTER_LINT_BANNED_FILE   a file with one term per line

Every pattern below is assembled from fragments, so this file contains no
literal example of anything it bans and does not flag itself.
"""

import os
import re
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", "__pycache__", "build", "dist", ".eggs", ".venv", "venv",
             ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = (".pyc", ".pyo")
# A local banned-terms list kept in the checkout (and ignored by git) holds the
# very terms it bans, so it is never scanned.
SKIP_FILES = {".lint-banned.local"}
# The build writes these into a source archive, with the line endings of the
# machine that built it. They are made from files this scan already reads.
GENERATED_AT_ROOT = {"PKG-INFO", "setup.cfg"}

ALLOWED_MAIL_DOMAIN = "example" + "." + "invalid"
FAMILIES = ["gem" + "ini", "cla" + "ude", "g" + "pt", "gr" + "ok"]
COLON = ":"
CR = chr(13)


def _build_patterns():
    users = "Us" + "ers"
    home = "ho" + "me"
    return [
        ("drive-letter path",
         re.compile(r"(?<![A-Za-z0-9])[A-Za-z]" + COLON + r"[\\/][\w.~$-]")),
        ("home directory path",
         re.compile("/(?:" + users + "|" + home + r")/[^/\s]+/")),
        ("windows profile path",
         re.compile(r"\\(?:" + users + r"|Documents and Settings)\\[^\\\s]+")),
        ("email address",
         re.compile(r"[\w.+-]+" + "@" + r"([\w-]+(?:\.[\w-]+)+)")),
        ("pinned model id",
         re.compile(r"(?<![\w-])(?:" + "|".join(FAMILIES) + r")-\d", re.IGNORECASE)),
    ]


PATTERNS = _build_patterns()


def banned_terms(env=None):
    """Extra terms from the environment and an optional file outside the repo."""
    env = os.environ if env is None else env
    terms = [t.strip() for t in env.get("BABYSITTER_LINT_BANNED", "").split(",")]
    listed = env.get("BABYSITTER_LINT_BANNED_FILE")
    if listed:
        terms += [line.strip() for line in Path(listed).read_text(encoding="utf-8").splitlines()]
    return [t for t in terms if t]


def scan_text(label, text, extra_terms=()):
    """Return 'label:line: class' findings for one file's text."""
    findings = []
    lowered_terms = [t.lower() for t in extra_terms]
    for number, line in enumerate(text.split("\n"), start=1):
        if CR in line:
            findings.append(f"{label}:{number}: carriage return (use LF line endings)")
        for name, pattern in PATTERNS:
            for found in pattern.finditer(line):
                if name == "email address" and found.group(1).lower() == ALLOWED_MAIL_DOMAIN:
                    continue
                findings.append(f"{label}:{number}: {name}")
                break
        lower = line.lower()
        for term in lowered_terms:
            if term in lower:
                findings.append(f"{label}:{number}: banned term")
    return findings


def repo_files(root=REPO_ROOT):
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(files):
            if name.endswith(SKIP_SUFFIXES) or name in SKIP_FILES:
                continue
            if name in GENERATED_AT_ROOT and Path(directory) == Path(root):
                continue
            yield Path(directory) / name


def scan_repo(root=REPO_ROOT, extra_terms=()):
    findings = []
    for path in repo_files(root):
        text = path.read_bytes().decode("utf-8", "replace")
        findings += scan_text(path.relative_to(root).as_posix(), text, extra_terms)
    return findings


class CleanRoomTest(unittest.TestCase):
    def test_the_repository_has_no_leaks(self):
        files = list(repo_files())
        # Prove the walk saw the repository, not an empty directory.
        names = {p.relative_to(REPO_ROOT).as_posix() for p in files}
        self.assertIn("agent_babysitter/enforce.py", names)
        self.assertIn("tests/test_clean_room.py", names)
        findings = scan_repo(extra_terms=banned_terms())
        self.assertEqual(findings, [], "clean-room leaks:\n" + "\n".join(findings))


class ScannerPositiveControlTest(unittest.TestCase):
    """The scanner must catch each leak class, or a clean result means nothing."""

    PLANTED = {
        "drive-letter path": "see " + "C" + COLON + "/" + "work/notes.txt",
        "drive-letter path (backslash)": "path=" + "D" + COLON + "\\" + "src\\app",
        "home directory path": "cd " + "/" + "home" + "/" + "alice" + "/project",
        "macOS user path": "open " + "/" + "Us" + "ers" + "/" + "bob" + "/x",
        "windows profile path": "dir " + "\\" + "Us" + "ers" + "\\" + "carol" + "\\Desktop",
        "email address": "contact " + "someone" + "@" + "corp" + ".test",
        "pinned model id (gemini)": "model " + FAMILIES[0] + "-" + "2.5-pro",
        "pinned model id (claude)": "model " + FAMILIES[1].upper() + "-" + "4",
        "pinned model id (gpt)": "model " + FAMILIES[2] + "-" + "4o",
        "pinned model id (grok)": "model " + FAMILIES[3] + "-" + "3",
        "carriage return": "line one" + CR,
    }

    def test_each_planted_leak_is_caught(self):
        for label, line in self.PLANTED.items():
            with self.subTest(leak=label):
                self.assertNotEqual(scan_text("planted", line), [], f"missed: {label}")

    def test_allowed_shapes_are_not_flagged(self):
        clean = [
            "builder" + "@" + ALLOWED_MAIL_DOMAIN,
            "https" + COLON + "//example.com/a",
            "model " + FAMILIES[0] + "-REPLACE-WITH-YOUR-MODEL",
            "path ~/.config/agent-babysitter/config.json",
        ]
        for line in clean:
            with self.subTest(line=line):
                self.assertEqual(scan_text("clean", line), [])

    def test_an_env_supplied_banned_term_is_caught(self):
        term = "Plan" + "ted" + "Corp"
        terms = banned_terms({"BABYSITTER_LINT_BANNED": " other , " + term})
        self.assertEqual(terms, ["other", term])
        found = scan_text("planted", "made by " + term.lower() + " staff", terms)
        self.assertEqual(found, ["planted:1: banned term"])

    def test_a_file_supplied_banned_term_is_caught(self):
        term = "Plan" + "ted" + "Host"
        with tempfile.TemporaryDirectory() as tmp:
            listed = Path(tmp) / "banned.txt"
            listed.write_bytes(("\n" + term + "\n").encode("utf-8"))
            terms = banned_terms({"BABYSITTER_LINT_BANNED_FILE": str(listed)})
        self.assertEqual(terms, [term])
        self.assertEqual(scan_text("planted", "on " + term.upper(), terms), ["planted:1: banned term"])

    def test_the_walk_skips_build_output_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            leak = "x " + "/" + "home" + "/" + "dave" + "/y\n"
            for rel in ("src/a.txt", "__pycache__/b.txt", "pkg.egg-info/c.txt", "build/d.txt",
                        ".git/e.txt", "f.pyc"):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(leak.encode("utf-8"))
            self.assertEqual(scan_repo(root), ["src/a.txt:1: home directory path"])


if __name__ == "__main__":
    unittest.main()
