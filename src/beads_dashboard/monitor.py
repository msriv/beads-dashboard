"""Independent project refreshes and an in-memory feed of observed changes."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from .beads import BeadsClient, Project
from .discovery import discover


@dataclass
class ProjectState:
    project: Project
    issues: dict[str, dict] = field(default_factory=dict)
    error: str | None = None
    last_success: datetime | None = None
    refreshing: bool = False
    duration: float = 0.0


@dataclass(frozen=True)
class Change:
    at: datetime
    project_key: str
    project_name: str
    issue_id: str
    title: str
    kind: str
    fields: tuple[str, ...] = ()


class Monitor:
    def __init__(self, roots: list[Path], projects: list[Path], *, client=None,
                 interval: float = 2.0, rescan_interval: float = 20.0):
        if not all(math.isfinite(value) and value > 0 for value in (interval, rescan_interval)):
            raise ValueError("Refresh and discovery intervals must be positive finite numbers")
        self.roots = roots
        self.projects = projects
        self.client = client if client is not None else BeadsClient()
        self.interval = interval
        self.rescan_interval = rescan_interval
        self.states: dict[str, ProjectState] = {}
        self.diagnostics: list[str] = []
        self.changes: deque[Change] = deque(maxlen=200)
        self.on_update: Callable[[], None] | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._available: set[str] = set()
        self._runner: asyncio.Task | None = None

    def _notify(self):
        if self.on_update is not None:
            self.on_update()

    async def scan(self, *, start_tasks: bool = False):
        paths, diagnostics = await asyncio.to_thread(discover, self.roots, self.projects)
        self.diagnostics = diagnostics
        old_by_path = {state.project.path: state for state in self.states.values()}

        async def resolve(path):
            try:
                project = await self.client.project(path)
                if start_tasks:
                    state = self.states.setdefault(project.key, ProjectState(project))
                    state.project = project
                    if project.key not in self._tasks or self._tasks[project.key].done():
                        self._tasks[project.key] = asyncio.create_task(self._poll(state), name="beads:" + project.key)
                    self._notify()
                return path, project, None
            except Exception as error:
                return path, None, str(error)

        resolved = await asyncio.gather(*(resolve(path) for path in paths))
        states: dict[str, ProjectState] = {}
        available: set[str] = set()
        # Prefer a readable alias when several worktrees point at the same data.
        for path, project, error in sorted(resolved, key=lambda item: item[1] is None):
            if project is None:
                state = old_by_path.get(path)
                if state is not None and state.project.key in available:
                    continue
                if state is None:
                    project = Project(key="unavailable:" + str(path), path=path, name=path.name)
                    state = ProjectState(project)
                state.error = error
                states[state.project.key] = state
                continue
            if project.key in states:
                continue
            state = self.states.get(project.key, ProjectState(project))
            state.project = project
            states[project.key] = state
            available.add(project.key)

        for key, old in self.states.items():
            if key not in states and old.project.path not in paths:
                old.error = "Workspace no longer discovered; retaining last known data"
                states[key] = old

        self.states = states
        self._available = available
        for key in list(self._tasks):
            if key not in available:
                task = self._tasks.pop(key)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if start_tasks:
            for key in available:
                if key not in self._tasks or self._tasks[key].done():
                    self._tasks[key] = asyncio.create_task(self._poll(states[key]), name="beads:" + key)
        self._notify()

    async def refresh(self, state: ProjectState):
        started = monotonic()
        state.refreshing = True
        try:
            issues = await self.client.snapshot(state.project)
            current = {issue["id"]: issue for issue in issues}
            now = datetime.now(timezone.utc)
            if state.last_success is not None:
                for issue_id in sorted(current.keys() | state.issues.keys()):
                    old, new = state.issues.get(issue_id), current.get(issue_id)
                    if old is None:
                        kind, fields = "created", ()
                    elif new is None:
                        kind, fields = "removed", ()
                    else:
                        fields = tuple(sorted(key for key in old.keys() | new.keys() if old.get(key) != new.get(key)))
                        if not fields:
                            continue
                        kind = "updated"
                    issue = new if new is not None else old
                    self.changes.append(Change(now, state.project.key, state.project.name,
                                               issue_id, str(issue.get("title", "")), kind, fields))
            state.issues = current
            state.last_success = now
            state.error = None
        except Exception as error:
            state.error = str(error)
        finally:
            state.duration = monotonic() - started
            state.refreshing = False
            self._notify()

    async def _poll(self, state: ProjectState):
        while True:
            await self.refresh(state)
            await asyncio.sleep(max(0.05, self.interval - state.duration))

    async def once(self):
        await self.scan()
        await asyncio.gather(*(self.refresh(self.states[key]) for key in sorted(self._available)))

    async def run(self):
        self._runner = asyncio.current_task()
        try:
            while True:
                await self.scan(start_tasks=True)
                await asyncio.sleep(self.rescan_interval)
        finally:
            await self._cancel_polls()
            self._runner = None

    async def _cancel_polls(self):
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self):
        if self._runner is not None and self._runner is not asyncio.current_task():
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        else:
            await self._cancel_polls()
