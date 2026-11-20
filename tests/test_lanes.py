"""Lane classification: which commands are routine, need a consult, or hit the hard gate."""

import tempfile
import unittest
from pathlib import Path

from agentsitter import config, lanes

LANE_1 = [
    "git status",
    "git log --grep=commit -5",
    "git diff --stat",
    "git add -A",
    "git checkout -b feature/x",
    "git stash",
    "git commit-graph write",
    "git rm old_module.py",
    "ls -la",
    "pytest -q",
    "npm test",
    "npm install",
    "npm ci",
    "pip install -r requirements.txt",
    "pip install -e .",
    "python script.py --flag",
    "rm -rf node_modules",
    "rm -rf scratch/out",
    "rm tmp/a.log",
    "mkdir build && rm -rf build/*",
    "curl https://example.com/api/status",
    "curl -X POST http://localhost:3000/hook -d x=1",
    "echo hello > out.txt",
    "cat > notes.md <<'EOF'\nremember to git push later\nEOF",
    "agentsitter consult --decision d1 --seat code --brief-file brief.md",
    # Guards against over-matching.
    "curl -f https://example.com/health",
    "curl -fsSL https://example.com/install.sh -o install.sh",
    "curl -D headers.txt https://example.com/",
    "curl -X POST http://localhost:3000/hook -d '{\"cb\":\"http://127.0.0.1/x\"}'",
    "cat > notes.md <<EOF\nplain words about git push\nEOF",
    "gh api repos/acme/demo/issues",
    "grep -rn pattern src 2>&1",
    "timeout 30 pytest -q",
    "env FOO=1 pytest -q",
    "/usr/bin/git status",
    "rm -rf build/a,b",
]

LANE_2 = [
    "npm install left-pad",
    "npm i -D typescript",
    "npm uninstall lodash",
    "pnpm add zod",
    "yarn add react",
    "pip install requests",
    "uv add httpx",
    "poetry add rich",
    "cargo add serde",
    "go get example.com/x/y",
    "alembic revision -m add_users",
    "python manage.py makemigrations",
    "npx prisma migrate dev",
    "git status; npm install left-pad",
]

LANE_3 = [
    "git commit -m x",
    "git -C repo commit -m x",
    "git commit --no-verify -m x",
    "git push",
    "git push --force origin main",
    "rtk git push",
    "FOO=1 git push",
    "cd repo && git push",
    'bash -c "git push"',
    "bash <<'EOF'\ngit push\nEOF",
    "git merge feature",
    "git rebase main",
    "git reset --hard HEAD~1",
    "git clean -fd",
    "git rm -f old_module.py",
    "gh pr merge 12",
    "npm publish",
    "twine upload dist/*",
    "docker push registry.example.com/img",
    "terraform apply",
    "kubectl apply -f deploy.yml",
    "vercel --prod",
    "wrangler deploy",
    "alembic upgrade head",
    "python manage.py migrate",
    "curl -X POST https://api.example.com/send -d x=1",
    "curl -d a=b https://example.com/form",
    "rm -rf src",
    "rm important.txt",
    "rm -rf /",
    "rm -rf ~",
    "rm -rf $DIR",
    "rm -rf ../other/node_modules",
    "cd .. && rm -rf build",
    "Remove-Item -Recurse -Force src",
    "find . -name '*.py' -delete",
    "doppler secrets set A=b",
    "gh secret set TOKEN",
    "echo X=1 >> .env",
    "sudo apt install x",
    "git config core.hooksPath /dev/null",
    "rm -rf .git/hooks",
    "ssh-keygen -Y sign -f key -n agentsitter file",
    "agentsitter approve 0123abcd",
    "python -m agentsitter approve 0123abcd",
    "AGENTSITTER_HOST_FAMILY=grok agentsitter consult --decision d --seat code --brief-file b",
    "npm install left-pad && git push",
    "agentsitter run -- npm publish",
    # A command named by its path, or with a quoted word, is the same command.
    "/usr/bin/git push",
    "/usr/bin/git commit --no-verify -m x",
    "C:\\Git\\cmd\\git.exe push",
    '"C:\\Program Files\\Git\\cmd\\git.exe" push',
    "/usr/local/bin/npm publish",
    "npm.cmd publish",
    '"git" push',
    "'npm' publish",
    "/usr/bin/sudo chmod 777 /etc/passwd",
    'git -C "my repo" push',
    "git -C/other/repo push",
    "git --git-dir /other/.git push",
    # A heredoc is dropped only when it is plainly data.
    "cat <<EOF\n$(rm -f important.txt)\nEOF",
    'echo "<<true"\nnpm publish\ntrue',
    "# <<true\ngit push\ntrue",
    "cat <<EOF | bash\nnpm publish\nEOF",
    # Deletions behind redirections, wrappers and PowerShell parameter forms.
    "2>&1 rm -rf src",
    "rm -rf 2>&1 src",
    "env -u FOO rm -rf src",
    "timeout 10s rm -rf src",
    "echo src | xargs -n 1 rm -rf",
    "Remove-Item -Path:src -Recurse -Force",
    "Remove-Item build/junk,important.txt -Force",
    'bash -lc "rm -rf src"',
    "bash -c \"echo 'ok'; rm -rf src\"",
    "find src -exec rm.exe -rf {} +",
    "Push-Location ..; Remove-Item -Recurse build",
    # Writes to other machines, however the option is spelled.
    "curl -X POST https://api.example.com/x -H 'Origin: http://localhost:3000'",
    "curl -X POST https://api.example.com/x -d '{\"cb\":\"http://localhost/cb\"}'",
    "curl -dfoo=bar https://example.com/api",
    "curl --request=DELETE https://example.com/object",
    "curl -Tartifact.zip https://example.com/upload",
    "wget --method POST --body-data=x https://example.com/api",
    "gh api repos/acme/demo/issues -f title=x",
    # Secrets files.
    "echo SECRET=1 > .env.production.local",
    "echo SECRET=1 >| .env",
    'Set-Content .env "SECRET=1"',
    # Variables that steer the consult or the gate.
    "ASK_GEMINI_CLI=/tmp/fake agentsitter consult --decision d --seat architecture --brief-file b",
    "CLAUDECODE= GEMINI_CLI=1 agentsitter consult --decision d --seat architecture --brief-file b",
    'env "AGENTSITTER_CONFIG=/tmp/x" git status',
    "cp evil.json ~/.config/agentsitter/config.json",
    # The pre-0.2.0 names, for an older release installed alongside this one.
    "babysitter approve 0123abcd",
    "agent-babysitter approve 0123abcd",
    "python -m agent_babysitter approve 0123abcd",
    "BABYSITTER_HOST_FAMILY=grok agentsitter consult --decision d --seat code --brief-file b",
    "$env:BABYSITTER_CONFIG = 'C:\\x.json'",
    "unset BABYSITTER_STATE_DIR",
    "cp evil.json ~/.config/agent-babysitter/config.json",
    "rm -rf ~/.local/state/agent-babysitter",
    # Found when the first round of fixes was reviewed.
    "2>& 1 rm -rf src",
    "cat <<EOF\n$\\\n(npm publish)\nEOF",
    "env -u A -u B rm -rf src",
    "env -u A /usr/bin/npm publish",
    "npm --ignore-scripts publish",
    "curl -sX POST https://example.com/",
    "Invoke-RestMethod -Method:Post -Uri https://example.com/ -Body x",
    "gh api -XDELETE repos/acme/demo",
    "gh api -ffield=value repos/acme/demo/issues",
    '"cd" .. && rm -rf build',
    "cat <<<x\ngit push",
]


class LaneTableTest(unittest.TestCase):
    def setUp(self):
        # The project must sit outside the scratch root, or every path in it is disposable.
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name) / "project"
        self.cwd.mkdir()
        self.scratch_root = Path(self._tmp.name) / "systemp"
        self.scratch_root.mkdir()
        self.cfg = config.merge(config.defaults(), {"scratch_roots": [str(self.scratch_root)]})

    def tearDown(self):
        self._tmp.cleanup()

    def lane(self, command):
        return lanes.classify(command, cwd=self.cwd, cfg=self.cfg).lane

    def test_routine_commands_are_lane_1(self):
        for command in LANE_1:
            with self.subTest(command=command):
                self.assertEqual(self.lane(command), 1)

    def test_non_trivial_commands_are_lane_2(self):
        for command in LANE_2:
            with self.subTest(command=command):
                self.assertEqual(self.lane(command), 2)

    def test_irreversible_commands_are_lane_3(self):
        for command in LANE_3:
            with self.subTest(command=command):
                self.assertEqual(self.lane(command), 3)


class LaneDetailTest(unittest.TestCase):
    def setUp(self):
        # The project must sit outside the scratch root, or every path in it is disposable.
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name) / "project"
        self.cwd.mkdir()
        self.scratch_root = Path(self._tmp.name) / "systemp"
        self.scratch_root.mkdir()
        self.cfg = config.merge(config.defaults(), {"scratch_roots": [str(self.scratch_root)]})

    def tearDown(self):
        self._tmp.cleanup()

    def test_commit_names_the_git_hook_that_enforces_it(self):
        result = lanes.classify("git commit -m x", cwd=self.cwd, cfg=self.cfg)
        self.assertEqual([m.git_hook for m in result.matches], ["pre-commit"])

    def test_no_verify_removes_the_git_hook_cover(self):
        result = lanes.classify("git commit --no-verify -m x", cwd=self.cwd, cfg=self.cfg)
        self.assertTrue(any(m.git_hook is None for m in result.matches))

    def test_lane_2_carries_the_seat_to_consult(self):
        result = lanes.classify("npm install left-pad", cwd=self.cwd, cfg=self.cfg)
        self.assertEqual(result.seat, "architecture")

    def test_higher_lane_wins_in_a_compound_command(self):
        result = lanes.classify("npm install left-pad && git push", cwd=self.cwd, cfg=self.cfg)
        self.assertEqual(result.lane, 3)
        self.assertEqual({m.lane for m in result.matches}, {2, 3})

    def test_deletion_inside_a_scratch_root_is_routine(self):
        target = self.scratch_root / "agentsitter-example" / "x"
        result = lanes.classify(f'rm -rf "{target.as_posix()}"', cwd=self.cwd, cfg=self.cfg)
        self.assertEqual(result.lane, 1)

    def test_deleting_the_scratch_root_itself_is_gated(self):
        target = self.scratch_root
        result = lanes.classify(f'rm -rf "{target.as_posix()}"', cwd=self.cwd, cfg=self.cfg)
        self.assertEqual(result.lane, 3)

    def test_config_can_add_a_hard_gate_rule(self):
        cfg = config.merge(self.cfg, {"rules": {"extra": [
            {"id": "custom-ship", "lane": 3, "category": "deploy", "pattern": r"\bmake\s+ship\b"}
        ]}})
        self.assertEqual(lanes.classify("make ship", cwd=self.cwd, cfg=cfg).lane, 3)

    def test_config_can_disable_a_rule_by_id(self):
        cfg = config.merge(self.cfg, {"rules": {"disable": ["db-migrate-apply"]}})
        self.assertEqual(lanes.classify("alembic upgrade head", cwd=self.cwd, cfg=cfg).lane, 1)


if __name__ == "__main__":
    unittest.main()
