"""Lane classification.

Lane 1: routine and reversible. Runs.
Lane 2: non-trivial. Needs one cross-family consult on record first.
Lane 3: irreversible or outward-facing. Needs a signed human approval.

Classification is deterministic and conservative: text is matched as shell,
the highest lane found wins, and anything that cannot be resolved is treated
as the more restrictive case.
"""

import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config as config_mod

# A command word must not be glued to a path or another word on its left,
# and a subcommand must not continue into a longer word on its right.
B = r"(?<![\w./\\-])"
E = r"(?![\w-])"
VALUE = r"(?:\"[^\"]*\"|'[^']*'|\S+)"
GIT = (B + r"git(?:\.exe)?(?:\s+(?:-[cC]\s*" + VALUE
       + r"|--(?:git-dir|work-tree|namespace|exec-path|config-env|super-prefix)(?:=|\s+)" + VALUE
       + r"|--[\w-]+(?:=\S+)?|-\w+))*\s+")
URL = re.compile(r"https?://[^\s'\"<>|;&)]+", re.IGNORECASE)
LOCAL_URL = re.compile(r"^https?://(?:localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)(?::\d+)?(?:[/?#]|$)", re.IGNORECASE)
WRITE_VERB = r"['\"]?(?:POST|PUT|PATCH|DELETE)"
NOT_TEMPLATE = r"(?!\.(?:example|sample|template|dist))"

BUILTIN_RULES = [
    # ---- lane 3: git history -------------------------------------------------
    {"id": "git-commit", "lane": 3, "category": "git", "git_hook": "pre-commit",
     "pattern": GIT + "commit" + E},
    {"id": "git-push", "lane": 3, "category": "git", "git_hook": "pre-push",
     "pattern": GIT + "push" + E},
    {"id": "git-rebase", "lane": 3, "category": "git", "git_hook": "pre-rebase",
     "pattern": GIT + "rebase" + E},
    {"id": "git-merge", "lane": 3, "category": "git", "pattern": GIT + "merge" + E},
    {"id": "git-history-rewrite", "lane": 3, "category": "git",
     "pattern": GIT + r"(?:reset\b.*\s--hard\b|(?:cherry-pick|revert|am|filter-branch|filter-repo|update-ref)" + E + ")"},
    {"id": "git-hook-bypass", "lane": 3, "category": "self-protection",
     "pattern": GIT + r"(?:(?:commit|push|merge|rebase)\b.*\s--no-v[\w-]*|commit\b.*\s-[a-zA-Z]*n[a-zA-Z]*(?:\s|$))"},
    {"id": "git-discard", "lane": 3, "category": "deletion",
     "pattern": GIT + r"(?:clean\b.*\s(?:-[a-zA-Z]*f|--force)|rm\b.*\s(?:-[a-zA-Z]*f[a-zA-Z]*|--force)(?:\s|$)|branch\b.*\s-D\b|stash\s+(?:drop|clear)\b)"},
    {"id": "gh-publish", "lane": 3, "category": "git",
     "pattern": B + r"gh\s+(?:pr\s+merge|release\s+(?:create|upload|delete|edit)|repo\s+(?:delete|rename|archive)|workflow\s+run|api\b.*(?:(?:-X|--method)[\s=]*" + WRITE_VERB + r"|\s(?:-[fF]\S*|--(?:field|raw-field|input)\b)))"},
    # ---- lane 3: deploy and publish -------------------------------------------
    {"id": "package-publish", "lane": 3, "category": "deploy",
     "pattern": B + r"(?:(?:npm|pnpm|yarn|bun)\s+(?:--?[\w-]+(?:=\S+)?\s+)*(?:npm\s+)?publish|twine\s+upload|poetry\s+publish|uv\s+publish|flit\s+publish|hatch\s+publish|cargo\s+publish|gem\s+push|dotnet\s+nuget\s+push|mvn\s+deploy)" + E},
    {"id": "image-push", "lane": 3, "category": "deploy",
     "pattern": B + r"(?:docker|podman)\s+(?:(?:image\s+)?push\b|.*\s--push\b)"},
    {"id": "platform-deploy", "lane": 3, "category": "deploy",
     "pattern": B + r"(?:wrangler\s+(?:pages\s+)?(?:deploy|publish)|vercel\s+deploy|vercel\b.*\s--prod\b|netlify\s+deploy|firebase\s+deploy|fly(?:ctl)?\s+deploy|serverless\s+deploy|sls\s+deploy|railway\s+up|cdk\s+(?:deploy|destroy)|sam\s+deploy|ansible-playbook)" + E},
    {"id": "infra-apply", "lane": 3, "category": "deploy",
     "pattern": B + r"(?:(?:terraform|tofu|terragrunt)\s+(?:\S+\s+)*?(?:apply|destroy)|pulumi\s+(?:up|destroy)|kubectl\s+(?:\S+\s+)*?(?:apply|delete|replace|patch|scale|rollout|drain|create|set)|helm\s+(?:install|upgrade|uninstall|rollback|delete))" + E},
    {"id": "cloud-cli-write", "lane": 3, "category": "deploy",
     "pattern": B + r"(?:gcloud\s+.*\b(?:deploy|delete)\b|aws\s+.*\b(?:deploy|s3\s+(?:sync|cp|mv|rm|rb)|(?:create|update|delete|put)-[\w-]+)|az\s+.*\b(?:create|delete|deploy|update)\b)"},
    {"id": "db-migrate-apply", "lane": 3, "category": "deploy",
     "pattern": r"(?:alembic\s+(?:upgrade|downgrade)|prisma\s+migrate\s+(?:deploy|reset)|prisma\s+db\s+push|manage\.py\s+migrate|db:migrate|flyway\s+migrate|liquibase\s+update|knex\s+migrate:(?:latest|up|rollback)|dbmate\s+(?:up|migrate)|goose\s+up)" + E},
    # ---- lane 3: effects outside the machine ----------------------------------
    # "local_only": the rule is waived when every URL in the command is this machine.
    # -d, -F and -T are matched in their own case: -f (fail) and -D (dump headers) write nothing.
    {"id": "http-write-curl", "lane": 3, "category": "external", "local_only": True,
     "pattern": B + r"curl(?:\.exe)?\b.*?(?:\s(?:(?-i:-[a-zA-Z]*X)|--request)[\s=]*" + WRITE_VERB + r"|\s(?-i:-[a-zA-Z]*[dFT])\S*(?:\s|$)|\s--(?:data(?:-[\w-]+)?|form(?:-string)?|upload-file|json)(?:\s|=|$))"},
    {"id": "http-write-wget", "lane": 3, "category": "external", "local_only": True,
     "pattern": B + r"wget\b.*\s--(?:(?:post|body)-(?:data|file)|method[=\s]+" + WRITE_VERB + ")"},
    {"id": "http-write-httpie", "lane": 3, "category": "external", "local_only": True,
     "pattern": B + r"https?\s+(?:--?\S+\s+)*(?:POST|PUT|PATCH|DELETE)\s"},
    {"id": "http-write-powershell", "lane": 3, "category": "external", "local_only": True,
     "pattern": r"\b(?:Invoke-RestMethod|Invoke-WebRequest|irm|iwr)\b.*-Method[\s:]+" + WRITE_VERB},
    {"id": "send-mail", "lane": 3, "category": "external",
     "pattern": B + r"(?:sendmail|msmtp|mutt|mailx|swaks|mail\s+-s)\b"},
    # ---- lane 3: secrets and privilege ----------------------------------------
    {"id": "secret-store-write", "lane": 3, "category": "secrets",
     "pattern": B + r"(?:doppler\s+secrets\s+(?:set|delete|upload)|vault\s+(?:kv\s+(?:put|patch|delete|destroy)|write|delete)|op\s+(?:item|vault)\s+(?:create|edit|delete)|aws\s+secretsmanager\s+(?:put|create|delete|update)|gcloud\s+secrets\s+(?:create|delete|versions\s+add)|az\s+keyvault\s+secret\s+(?:set|delete)|kubectl\s+create\s+secret|gh\s+(?:secret|variable)\s+(?:set|delete|remove))"},
    {"id": "env-file-write", "lane": 3, "category": "secrets",
     "pattern": r"(?:(?:>>?\|?|\btee\b(?:\s+-\S+)*)\s*['\"]?[^\s'\"]*\.env" + NOT_TEMPLATE + r"(?:\.[\w-]+)*['\"]?(?:\s|$)|" + B + r"(?:cp|mv|sed\s+-i\S*|install|Set-Content|Add-Content|Out-File|sc|ac)\b.*\s['\"]?[^\s'\"]*\.env" + NOT_TEMPLATE + r"(?:\.[\w-]+)*['\"]?(?:\s|$))"},
    {"id": "privilege-elevation", "lane": 3, "category": "secrets",
     "pattern": B + r"(?:sudo|doas|runas|pkexec)\b|-Verb\s+RunAs\b"},
    # ---- lane 3: destruction the path check cannot resolve --------------------
    {"id": "delete-in-subshell", "lane": 3, "category": "deletion",
     "pattern": r"(?:\$\(|`|(?:\s-[a-zA-Z]*c|-Command|/c)\s+['\"]).*?" + B + r"(?:rm|rmdir|rd|unlink|del|erase|shred|Remove-Item|ri)\s"},
    {"id": "disk-destructive", "lane": 3, "category": "deletion",
     "pattern": B + r"(?:mkfs\S*|dd\s+.*\bof=|diskpart|format\s+[a-zA-Z]:)"},
    # ---- lane 3: the governance system protecting itself -----------------------
    {"id": "self-approval", "lane": 3, "category": "self-protection",
     "pattern": B + r"(?:agent[-_])?babysitter(?:\.exe)?\s+approve\b|" + B + r"ssh-keygen(?:\.exe)?\b.*\s-Y\s+sign\b"},
    {"id": "governance-env", "lane": 3, "category": "self-protection",
     "pattern": r"(?:^|[\s;&|(])(?:export\s+|set\s+)?(?:BABYSITTER_\w+|ADVISOR_DEPTH|ASK_\w+|CLAUDECODE|GEMINI_CLI)\s*=|\$env:(?:BABYSITTER_\w+|ADVISOR_DEPTH|ASK_\w+|CLAUDECODE|GEMINI_CLI)\s*=|\b(?:unset|env\s+-u)\s+(?:BABYSITTER_\w+|ADVISOR_DEPTH|CLAUDECODE|GEMINI_CLI)\b"},
    {"id": "governance-files", "lane": 3, "category": "self-protection",
     "pattern": r"core\.hooksPath|\.git[/\\]hooks|allowed_signers|(?:\.config|\.local[/\\]state)[/\\]agent-babysitter"},
    # ---- lane 2: decisions worth a second opinion -------------------------------
    {"id": "dependency-node", "lane": 2, "category": "dependency", "seat": "architecture",
     "pattern": B + r"(?:(?:npm|pnpm|bun)\s+(?:install|i|add)\s+(?:-\S+\s+)*[^-\s]\S*|(?:npm|pnpm|yarn|bun)\s+(?:add|uninstall|remove|rm|un)" + E + ")"},
    {"id": "dependency-python", "lane": 2, "category": "dependency", "seat": "architecture",
     "pattern": B + r"(?:(?:pip3?|python\S*\s+-m\s+pip|uv\s+pip|pipenv|conda|mamba)\s+install(?!.*\s(?:-r|--requirement|-e|--editable)\b)\s+(?:-\S+\s+)*[^-\s]\S*|(?:pip3?|pipenv)\s+uninstall\b|(?:uv|poetry|pdm)\s+(?:add|remove)" + E + ")"},
    {"id": "dependency-other", "lane": 2, "category": "dependency", "seat": "architecture",
     "pattern": B + r"(?:cargo\s+(?:add|remove|rm)|go\s+get|gem\s+install|bundle\s+add|composer\s+(?:require|remove)|dotnet\s+add\s+(?:\S+\s+)?package|brew\s+install|apt(?:-get)?\s+install|choco\s+install|winget\s+install|scoop\s+install)" + E},
    {"id": "schema-authoring", "lane": 2, "category": "schema", "seat": "architecture",
     "pattern": r"(?:alembic\s+revision|manage\.py\s+makemigrations|prisma\s+migrate\s+dev|rails\s+g(?:enerate)?\s+migration|knex\s+migrate:make|typeorm\s+migration:(?:create|generate)|dbmate\s+new)" + E},
]

PACKAGE_DIR = Path(__file__).resolve().parent


def _package_spellings():
    """How a shell command may write the path of this package, lower-cased."""
    text = PACKAGE_DIR.as_posix().lower()
    drive = re.match(r"^([a-z]):/", text)
    return {text, f"/{drive.group(1)}/{text[3:]}"} if drive else {text}


DELETE_WORDS = {"rm", "rmdir", "unlink", "shred", "del", "erase", "rd", "remove-item", "ri"}
CMD_STYLE_DELETE = {"del", "erase", "rd", "rmdir"}
POWERSHELL_DELETE = {"remove-item", "ri", "del", "erase", "rd"}
CHDIR_WORDS = {"cd", "pushd", "popd", "chdir", "set-location", "sl", "push-location", "pop-location"}
PREFIX_WORDS = {
    "sudo", "doas", "env", "command", "builtin", "exec", "nohup", "time", "nice",
    "stdbuf", "timeout", "rtk", "then", "do", "else", "elif", "if", "while",
    "until", "!", "{",
}
HEREDOC_SHELLS = {
    "bash", "sh", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "cmd",
    "eval", "source", ".", "ssh", "xargs", "sudo",
}
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
REDIRECT = re.compile(r"^(?:\d*|&)[<>]")
BARE_REDIRECT = re.compile(r"^(?:\d*|&)[<>]{1,3}[|&]?$")
SCRIPT_SUFFIX = re.compile(r"\.(?:exe|cmd|bat|ps1)$", re.IGNORECASE)
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
GLOB_CHARS = "*?[{"


@dataclass(frozen=True)
class Match:
    rule_id: str
    lane: int
    category: str
    segment: str
    seat: Optional[str] = None
    git_hook: Optional[str] = None


@dataclass(frozen=True)
class Classification:
    command: str
    lane: int
    matches: tuple

    @property
    def seat(self):
        for match in self.matches:
            if match.lane == 2:
                return match.seat or "code"
        return "code"


def active_rules(cfg):
    disabled = set(cfg["rules"]["disable"])
    rules = [r for r in BUILTIN_RULES if r["id"] not in disabled]
    rules += [r for r in cfg["rules"]["extra"] if r["id"] not in disabled]
    return rules, disabled


def _first_word(text):
    for token in text.split():
        if ASSIGNMENT.match(token):
            continue
        return os.path.basename(token).lower().removesuffix(".exe")
    return ""


def strip_heredocs(command):
    """Drop heredoc bodies, unless the heredoc is fed to something that runs it."""
    lines = command.split("\n")
    kept, i = [], 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        found = HEREDOC.search(line)
        if found:
            end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == found.group(2)), None)
            if end is not None and _heredoc_is_data(line, found, lines[i + 1:end]):
                i = end + 1
                continue
        i += 1
    return "\n".join(kept)


def _heredoc_is_data(line, found, body):
    """True only when the heredoc body is plainly text that nothing will run."""
    before, quote = line[: found.start()], None
    for index, ch in enumerate(before):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (index == 0 or before[index - 1].isspace()):
            return False  # `<<` in a comment starts no heredoc
    if quote:
        return False  # `<<` inside a quoted string starts no heredoc
    if _first_word(re.split(r"[;&|(]", before)[-1]) in HEREDOC_SHELLS:
        return False  # the body is fed to something that runs it
    if re.search(r"[|;&`]|\$\(", line[found.end():]):
        return False  # piped or chained onward: what runs next may run the body
    if not found.group(1) and any("$" in text or "`" in text for text in body):
        return False  # an unquoted delimiter lets the shell expand, and so run, the body
    return True


def split_segments(text):
    """Split into simple commands on shell separators that sit outside quotes."""
    segments, buf, quote, i = [], [], None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                buf.append(text[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
            buf.append(ch)
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch == "\\" and i + 1 < len(text):
            buf.append(text[i:i + 2])
            i += 2
            continue
        elif ch == "|" and buf and buf[-1] == ">":
            buf.append(ch)  # `>|` is one redirection, not a pipe
        elif ch in ";|\n()`" or (ch == "&" and not (buf and buf[-1] in "<>") and text[i + 1:i + 2] != ">"):
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    segments.append("".join(buf))
    if quote:  # unbalanced quote: add a quote-blind split so nothing hides in the tail
        segments += re.split(r"[;|&\n()`]", text)
    return [s.strip() for s in segments if s.strip()]


def _tokens(segment):
    try:
        raw = shlex.split(segment, posix=False)
    except ValueError:
        raw = segment.split()
    out, skip = [], False
    for token in raw:
        if skip:
            skip = False
            continue
        if REDIRECT.match(token):
            skip = bool(BARE_REDIRECT.match(token))  # `>` waits for its target; `2>&1` carries it
            continue
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
            token = token[1:-1]
        out.append(token)
    return out


def _word(token):
    """A command token reduced to its name: no directory, no .exe/.cmd suffix, lower case."""
    return SCRIPT_SUFFIX.sub("", os.path.basename(token.replace("\\", "/"))).lower()


def _command_and_args(segment):
    """Return (command word, args, piped) after stripping wrappers and assignments."""
    tokens = _tokens(segment)
    piped = wrapped = False
    while tokens:
        word = _word(tokens[0])
        if ASSIGNMENT.match(tokens[0]):
            tokens = tokens[1:]
        elif word == "xargs" or word in PREFIX_WORDS:
            piped, wrapped = piped or word == "xargs", True
            tokens = tokens[1:]
            while tokens and (tokens[0].startswith("-") or tokens[0].isdigit()):
                tokens = tokens[1:]
        else:
            # A wrapper's own option value (env -u NAME, timeout 10s, xargs -I {}) sits
            # where the command should be. If a deletion follows it, judge the deletion.
            if wrapped and word not in DELETE_WORDS | {"find"}:
                for index, token in enumerate(tokens[1:], 1):
                    if _word(token) in DELETE_WORDS | {"find"}:
                        return _word(token), tokens[index + 1:], piped
            return word, tokens[1:], piped
    return "", [], piped


def _normalized(segment):
    """The segment as the shell sees it: words unquoted, the command named without its path.

    Rules are matched against this as well as the raw text, so `/usr/bin/git push`,
    `"git" push` and `npm.cmd publish` are the commands they plainly are.
    """
    out, command_seen, wrapped = [], False, False
    for token in _tokens(segment):
        plain = not ASSIGNMENT.match(token) and not token.startswith("-") and not token.isdigit()
        if plain and not command_seen:
            token = _word(token) if re.search(r"[/\\.]", token) else token
            command_seen = token.lower() not in PREFIX_WORDS and token.lower() != "xargs"
            wrapped = wrapped or not command_seen
        elif plain and wrapped and re.search(r"[/\\]", token):
            # After a wrapper, its option values hide which word is the command
            # (env -u NAME /usr/bin/npm publish), so later paths are reduced as well.
            token = _word(token)
        out.append(f'"{token}"' if re.search(r"\s", token) else token)
    return " ".join(out)


def _only_local(text):
    """True when the text names at least one URL and every URL is this machine."""
    urls = URL.findall(text)
    return bool(urls) and all(LOCAL_URL.match(url) for url in urls)


def project_root(cwd):
    """The git toplevel containing cwd, or cwd itself outside a repository."""
    try:
        done = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if done.returncode == 0 and done.stdout.strip():
            return Path(done.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd)


def _norm(path):
    return os.path.normcase(os.path.realpath(str(path)))


def _is_under(path, parent, strict):
    path, parent = _norm(path), _norm(parent)
    if path == parent:
        return not strict
    return path.startswith(parent.rstrip("\\/") + os.sep)


def _is_disposable(target, cwd, root, cfg, chdir_seen):
    """True only when the deletion provably stays inside scratch space."""
    if not target or target.startswith("~") or any(ch in target for ch in "$`%"):
        return False
    parts = target.replace("\\", "/").split("/")
    while parts and any(ch in parts[-1] for ch in GLOB_CHARS):
        parts.pop()  # `build/*` empties build; judge build itself
    if any(ch in part for part in parts for ch in GLOB_CHARS):
        return False
    cleaned = "/".join(parts) or "."
    if os.name == "nt":
        drive = re.match(r"^/([a-zA-Z])(/|$)", cleaned)
        if drive:
            cleaned = f"{drive.group(1)}:/{cleaned[3:]}"
        elif cleaned.startswith("/"):
            return False  # a POSIX-rooted path has no fixed meaning here
    absolute = cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned)
    if not absolute and chdir_seen:
        return False
    full = Path(cleaned) if absolute else Path(cwd) / cleaned
    for scratch_root in cfg["scratch_roots"]:
        base = tempfile.gettempdir() if scratch_root == config_mod.SYSTEM_TEMP else scratch_root
        if _is_under(full, base, strict=True):
            return True
    if not _is_under(full, root, strict=True):
        return False
    relative = Path(os.path.relpath(_norm(full), _norm(root)))
    names = {name.lower() for name in cfg["scratch_dirs"]}
    return any(part.lower() in names for part in relative.parts)


def _deletion_targets(word, args, piped):
    """Paths a segment deletes, or None when the segment deletes nothing."""
    if word in DELETE_WORDS:
        if piped:
            return [""]  # targets arrive on stdin: unresolvable
        targets, literal = [], False
        for arg in args:
            if arg == "--":
                literal = True
            elif not literal and arg.startswith("-"):
                bound = re.match(r"^-[A-Za-z]+[:=](.+)$", arg)  # PowerShell -Path:value
                if bound:
                    targets.extend(bound.group(1).split(","))
            elif not literal and word in CMD_STYLE_DELETE and re.match(r"^/[a-zA-Z?]$", arg):
                continue
            elif word in POWERSHELL_DELETE:
                targets.extend(part for part in arg.split(",") if part)  # a PowerShell list
            else:
                targets.append(arg)
        return targets or None
    if word == "find":
        deletes = "-delete" in args or any(
            flag in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < len(args)
            and _word(args[i + 1]) in DELETE_WORDS
            for i, flag in enumerate(args)
        )
        if not deletes:
            return None
        starts = []
        for arg in args:
            if arg in ("-H", "-L", "-P") and not starts:
                continue
            if arg.startswith("-") or arg in ("(", "!"):
                break
            starts.append(arg)
        return starts or ["."]
    return None


def classify(command, cwd, cfg):
    """Classify one shell command line. Returns a Classification."""
    rules, disabled = active_rules(cfg)
    matches, chdir_seen, root = [], False, None
    for segment in split_segments(strip_heredocs(command)):
        forms = [segment]
        normalized = _normalized(segment)
        if normalized and normalized != segment:
            forms.append(normalized)
        for rule in rules:
            for text in forms:
                if not re.search(rule["pattern"], text, re.IGNORECASE):
                    continue
                if rule.get("unless") and re.search(rule["unless"], text, re.IGNORECASE):
                    continue
                if rule.get("local_only") and _only_local(text):
                    continue
                matches.append(Match(rule["id"], rule["lane"], rule.get("category", "custom"),
                                     segment, rule.get("seat"), rule.get("git_hook")))
                break
        slashed = segment.replace("\\", "/")
        if "protected-path" not in disabled and any(
                re.search(pattern, slashed, re.IGNORECASE) for pattern in cfg["protected_paths"]):
            matches.append(Match("protected-path", 3, "self-protection", segment))
        if "governance-code" not in disabled and any(
                spelling in slashed.lower() for spelling in _package_spellings()):
            matches.append(Match("governance-code", 3, "self-protection", segment))
        word, args, piped = _command_and_args(segment)
        targets = _deletion_targets(word, args, piped)
        if targets and "delete-outside-scratch" not in disabled:
            root = root or project_root(cwd)
            if not all(_is_disposable(t, cwd, root, cfg, chdir_seen) for t in targets):
                matches.append(Match("delete-outside-scratch", 3, "deletion", segment))
        if word in CHDIR_WORDS:
            chdir_seen = True
    lane = max((m.lane for m in matches), default=1)
    return Classification(command, lane, tuple(matches))
