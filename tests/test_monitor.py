import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from beads_dashboard.monitor import Monitor


class FakeClient:
    def __init__(self):
        self.values = {}
        self.errors = {}
        self.identities = {}
        self.calls = {}
        self.waits = {}
        self.project_errors = {}
        self.project_waits = {}

    async def project(self, path):
        if path.name in self.project_waits:
            await self.project_waits[path.name].wait()
        if path.name in self.project_errors:
            raise RuntimeError(self.project_errors[path.name])
        return SimpleNamespace(key=self.identities.get(path.name, str(path)), path=path, name=path.name, branch="main")

    async def snapshot(self, project):
        name = project.name
        self.calls[name] = self.calls.get(name, 0) + 1
        if name in self.waits:
            await self.waits[name].wait()
        if name in self.errors:
            raise RuntimeError(self.errors[name])
        return [dict(issue) for issue in self.values.get(name, [])]


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ("alpha", "beta"):
            (self.root / name / ".beads").mkdir(parents=True)
        self.client = FakeClient()
        self.client.values = {"alpha": [{"id": "same", "title": "Alpha", "status": "open"}], "beta": [{"id": "same", "title": "Beta", "status": "open"}]}
        self.monitor = Monitor([self.root], [], client=self.client, interval=0.02, rescan_interval=0.04)

    async def asyncTearDown(self):
        await self.monitor.stop()
        self.tmp.cleanup()

    def state(self, name):
        return next(state for state in self.monitor.states.values() if state.project.name == name)

    async def test_initial_snapshot_is_baseline_and_same_ids_are_distinct(self):
        await self.monitor.once()
        self.assertEqual(len(self.monitor.states), 2)
        self.assertEqual(self.state("alpha").issues["same"]["title"], "Alpha")
        self.assertEqual(self.state("beta").issues["same"]["title"], "Beta")
        self.assertFalse(self.monitor.changes)
        self.assertIsNotNone(self.state("alpha").last_success)

    async def test_diffs_create_update_and_removal_without_startup_flood(self):
        await self.monitor.once()
        state = self.state("alpha")
        self.client.values["alpha"] = [{"id": "same", "title": "Changed", "status": "in_progress"}, {"id": "new", "title": "New"}]
        await self.monitor.refresh(state)
        self.assertEqual({change.kind for change in self.monitor.changes}, {"updated", "created"})
        change = next(change for change in self.monitor.changes if change.kind == "updated")
        self.assertIn("title", change.fields)
        self.assertEqual(change.project_key, state.project.key)
        self.client.values["alpha"] = []
        await self.monitor.refresh(state)
        self.assertEqual(self.monitor.changes[-1].kind, "removed")

    async def test_error_retains_last_known_data_and_recovers(self):
        await self.monitor.once()
        state = self.state("alpha")
        last_success = state.last_success
        self.client.errors["alpha"] = "database locked"
        await self.monitor.refresh(state)
        self.assertEqual(state.error, "database locked")
        self.assertEqual(state.last_success, last_success)
        self.assertIn("same", state.issues)
        del self.client.errors["alpha"]
        await self.monitor.refresh(state)
        self.assertIsNone(state.error)

    async def test_shared_backend_deduplicates_but_distinct_branches_do_not(self):
        self.client.identities = {"alpha": "shared-main", "beta": "shared-main"}
        await self.monitor.once()
        self.assertEqual(len(self.monitor.states), 1)
        self.client.identities["beta"] = "shared-feature"
        await self.monitor.once()
        self.assertEqual(set(self.monitor.states), {"shared-main", "shared-feature"})

    async def test_slow_project_does_not_delay_healthy_refreshes(self):
        self.client.waits["alpha"] = asyncio.Event()
        runner = asyncio.create_task(self.monitor.run())
        try:
            async with asyncio.timeout(2):
                while self.client.calls.get("beta", 0) < 3:
                    await asyncio.sleep(0.01)
            self.assertEqual(self.client.calls.get("alpha"), 1)
            self.assertIsNotNone(self.state("beta").last_success)
        finally:
            await self.monitor.stop()
            await asyncio.gather(runner, return_exceptions=True)
        self.assertFalse(any(state.refreshing for state in self.monitor.states.values()))

    async def test_healthy_alias_takes_precedence_when_another_path_is_unreadable(self):
        self.client.identities = {"alpha": "shared", "beta": "shared"}
        await self.monitor.once()
        self.client.project_errors["alpha"] = "path unavailable"
        await self.monitor.once()
        self.assertEqual(len(self.monitor.states), 1)
        state = self.monitor.states["shared"]
        self.assertIsNone(state.error)
        self.assertEqual(state.project.name, "beta")

    async def test_slow_identity_does_not_delay_first_healthy_snapshot(self):
        self.client.project_waits["alpha"] = asyncio.Event()
        runner = asyncio.create_task(self.monitor.run())
        try:
            async with asyncio.timeout(0.3):
                while self.client.calls.get("beta", 0) < 2:
                    await asyncio.sleep(0.01)
            self.assertIsNotNone(self.state("beta").last_success)
            self.assertNotIn("alpha", self.client.calls)
        finally:
            await self.monitor.stop()
            await asyncio.gather(runner, return_exceptions=True)

    async def test_rescan_finds_new_projects_and_keeps_missing_data_visibly_stale(self):
        await self.monitor.once()
        (self.root / "gamma" / ".beads").mkdir(parents=True)
        (self.root / "alpha" / ".beads").rmdir()
        (self.root / "alpha").rmdir()
        await self.monitor.once()
        self.assertIn("gamma", {state.project.name for state in self.monitor.states.values()})
        self.assertIn("same", self.state("alpha").issues)
        self.assertIn("discovered", self.state("alpha").error)

    async def test_invalid_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            Monitor([], [], interval=0)


if __name__ == "__main__":
    unittest.main()
