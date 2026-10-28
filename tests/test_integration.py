"""Opt-in proof against disposable real Beads databases, never user projects."""

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from time import monotonic

from beads_dashboard.beads import BeadsClient, BeadsError
from beads_dashboard.monitor import Monitor


@unittest.skipUnless(os.environ.get("BD_DASHBOARD_INTEGRATION") == "1" and shutil.which("bd"),
                     "set BD_DASHBOARD_INTEGRATION=1 to exercise real bd")
class RealBeadsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="beads-dashboard-proof-")
        self.root = Path(self.tmp.name)
        self.alpha, self.beta = self.root / "alpha", self.root / "beta"
        self.monitor = Monitor([self.root], [], interval=0.1, rescan_interval=0.5)
        for project in (self.alpha, self.beta):
            project.mkdir()
            await self.command(project, "git", "init", "-b", "main")
            await self.command(project, "git", "config", "user.name", "Dashboard Test")
            await self.command(project, "git", "config", "user.email", "dashboard@example.invalid")
            await self.bd(project, "init", "--prefix", "board", "--non-interactive", "--skip-hooks", "--skip-agents")
            await self.bd(project, "create", "--id", "board-one", "--title", project.name + " task",
                          "--description", "Disposable integration fixture", "--assignee", "agent-one")

    async def asyncTearDown(self):
        await self.monitor.stop()
        self.tmp.cleanup()

    async def command(self, directory, *args):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("BEADS_", "BD_", "GIT_"))}
        process = await asyncio.create_subprocess_exec(*args, cwd=directory, env=environment,
                                                       stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace") + stdout.decode(errors="replace"))
        return stdout.decode()

    async def bd(self, directory, *args):
        return await self.command(directory, "bd", "--sandbox", "--json", *args)

    @staticmethod
    def hashes(project):
        return {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (project / ".beads").rglob("*") if path.is_file()}

    def state(self, name):
        return next(state for state in self.monitor.states.values() if state.project.name == name)

    async def test_multi_project_live_changes_worktrees_and_nonmutation(self):
        bulk = self.root / "bulk.md"
        bulk.write_text("\n".join(f"## Bulk task {index}\n\nA disposable task.\n" for index in range(52)))
        await self.bd(self.alpha, "create", "--file", str(bulk))
        await self.bd(self.alpha, "create", "--id", "board-blocker", "--title", "Blocker", "--description", "Fixture")
        await self.bd(self.alpha, "dep", "add", "board-one", "board-blocker")
        await self.bd(self.alpha, "comment", "board-one", "Recorded test comment")

        before_alpha, before_beta = self.hashes(self.alpha), self.hashes(self.beta)
        started = monotonic()
        await self.monitor.once()
        self.assertEqual(len(self.monitor.states), 2)
        alpha = self.state("alpha")
        self.assertEqual(len(alpha.issues), 54)
        self.assertEqual(len(self.state("beta").issues), 1)
        self.assertTrue(alpha.issues["board-one"]["_blocked"])
        self.assertFalse(alpha.issues["board-one"]["_ready"])
        self.assertFalse(self.monitor.changes)
        detail = await self.monitor.client.detail(alpha.project, "board-one")
        self.assertIn("Recorded test comment", json.dumps(detail["comments"]))
        self.assertIn("board-blocker", json.dumps(detail["dependencies"]))
        self.assertEqual(self.hashes(self.alpha), before_alpha)
        self.assertEqual(self.hashes(self.beta), before_beta)
        print(f"\nReal bd: 2 projects / 55 tickets initial scan + full detail: {monotonic() - started:.2f}s", flush=True)

        runner = asyncio.create_task(self.monitor.run())
        try:
            await self.bd(self.alpha, "update", "board-one", "--status", "in_progress", "--assignee", "agent-two")
            await self.bd(self.alpha, "close", "board-blocker", "--reason", "Fixture resolved")
            async with asyncio.timeout(30):
                while not (alpha.issues["board-one"].get("status") == "in_progress"
                           and alpha.issues["board-one"].get("assignee") == "agent-two"
                           and not alpha.issues["board-one"].get("_blocked")):
                    await asyncio.sleep(0.1)
            self.assertTrue(any(change.issue_id == "board-one" for change in self.monitor.changes))
            self.assertIsNotNone(self.state("beta").last_success)
            await self.bd(self.beta, "--actor", "agent-one", "close", "board-one", "--reason", "Fixture complete")
            async with asyncio.timeout(30):
                while self.state("beta").issues["board-one"].get("status") != "closed":
                    await asyncio.sleep(0.1)
        finally:
            await self.monitor.stop()
            await asyncio.gather(runner, return_exceptions=True)

        worktree = self.root / "alpha-worktree"
        await self.command(self.alpha, "git", "worktree", "add", "-b", "feature", str(worktree))
        shared = await self.monitor.client.project(worktree)
        self.assertEqual(shared.key, alpha.project.key)
        await self.monitor.once()
        self.assertEqual(len(self.monitor.states), 2)

        # A removed local workspace must never fall back to an ancestor database.
        missing_beads = self.beta / ".beads"
        hidden_beads = self.beta / ".beads-unavailable"
        missing_beads.rename(hidden_beads)
        try:
            await self.monitor.refresh(self.state("beta"))
            self.assertIsNotNone(self.state("beta").error)
            self.assertEqual(self.state("beta").issues["board-one"]["status"], "closed")
            with self.assertRaises(BeadsError):
                await self.monitor.client.detail(self.state("beta").project, "board-one")
        finally:
            hidden_beads.rename(missing_beads)
        await self.monitor.refresh(self.state("beta"))
        self.assertIsNone(self.state("beta").error)


if __name__ == "__main__":
    unittest.main()
