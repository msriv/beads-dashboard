"""Read-only, cancellable access to the installed Beads CLI."""

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path


class BeadsError(Exception):
    """A Beads failure suitable for showing in the dashboard."""


@dataclass(frozen=True)
class Project:
    key: str
    path: Path
    name: str
    branch: str = ''


class BeadsClient:
    """Return native Beads objects; snapshot adds `_ready` and `_blocked` booleans.

    Those annotations come from Beads' own queries, rather than stored status.
    Optional detail fields stay absent when Beads does not supply them.
    """

    _INCLUDE = ('--all', '--limit', '0', '--include-ephemeral', '--include-gates',
                '--include-infra', '--include-templates')

    def __init__(self, timeout: float = 5):
        self.timeout = timeout
        self._locks: dict[str, asyncio.Lock] = {}
        self._identities: dict[str, str] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(self._identities.get(key, key), asyncio.Lock())

    @staticmethod
    def _check_path(path: Path) -> None:
        if not (path / '.beads').is_dir():
            raise BeadsError(f'{path}: local .beads directory is missing or unavailable')

    async def project(self, path: Path) -> Project:
        return await self._project(path, lock_branch=True)

    async def _project(self, path: Path, lock_branch: bool = False) -> Project:
        path = path.expanduser().resolve()
        self._check_path(path)
        context = await self._run(path, 'context')
        if (not isinstance(context, dict) or context.get('backend') != 'dolt'
                or any(not isinstance(context.get(key), str) or not context[key]
                       for key in ('database', 'beads_dir', 'project_id'))
                or ('repo_root' in context and not isinstance(context['repo_root'], str))):
            raise BeadsError(f'{path}: bd context did not provide a supported database identity')
        mode = context.get('dolt_mode')
        if mode == 'embedded':
            location = [str(Path(context['beads_dir']).resolve()), context['project_id']]
        elif mode == 'server':
            if not context.get('server_host') or not context.get('server_port'):
                raise BeadsError(f'{path}: bd context did not provide a server identity')
            location = [context['server_host'], context['server_port']]
        else:
            raise BeadsError(f'{path}: unsupported Beads mode {mode!r}; monitoring must not start a server')
        identity = [context['backend'], mode, location, context['database']]
        # Embedded clones can share project IDs while holding independent working data.
        if lock_branch:
            async with self._lock(json.dumps(identity)):
                branches = await self._run(path, 'branch')
        else:
            branches = await self._run(path, 'branch')
        if not isinstance(branches, dict) or not isinstance(branches.get('current'), str) or not branches['current']:
            raise BeadsError(f'{path}: Beads did not report the current Dolt branch')
        branch = branches['current']
        name = Path(context.get('repo_root') or path).name
        key = json.dumps([*identity, branch])
        self._identities[key] = json.dumps(identity)
        return Project(key, path, name, branch)

    async def _validate(self, project: Project) -> None:
        current = await self._project(project.path)
        if current.key != project.key:
            raise BeadsError(f'{project.path}: Beads working-data identity changed; waiting for project rediscovery')

    async def _validate_branch(self, project: Project) -> None:
        branches = await self._run(project.path, 'branch')
        if not isinstance(branches, dict) or branches.get('current') != project.branch:
            raise BeadsError(f'{project.path}: Dolt branch changed during read; waiting for project rediscovery')

    async def snapshot(self, project: Project) -> list[dict]:
        async with self._lock(project.key):
            await self._validate(project)
            issues = self._issues(await self._run(project.path, 'list', *self._INCLUDE))
            ready = self._issues(await self._run(project.path, 'list', *self._INCLUDE, '--ready'))
            blocked = self._issues(await self._run(project.path, 'blocked'))
            await self._validate_branch(project)
        ready_ids = {issue['id'] for issue in ready}
        blocked_ids = {issue['id'] for issue in blocked}
        return [dict(issue, _ready=issue['id'] in ready_ids, _blocked=issue['id'] in blocked_ids)
                for issue in issues]

    async def detail(self, project: Project, issue_id: str) -> dict:
        async with self._lock(project.key):
            await self._validate(project)
            issues = self._issues(await self._run(project.path, 'show', f'--id={issue_id}',
                                                  '--long', '--include-comments', '--include-dependents'))
            await self._validate_branch(project)
        for issue in issues:
            if issue['id'] == issue_id:
                return issue
        raise BeadsError(f'Beads issue {issue_id} is unavailable')

    @staticmethod
    def _issues(value) -> list[dict]:
        if value is None:
            return []
        if not isinstance(value, list) or any(not isinstance(issue, dict) or not isinstance(issue.get('id'), str) or not issue['id'] for issue in value):
            raise BeadsError('Beads returned invalid issue JSON')
        return value

    @staticmethod
    def _environment() -> dict[str, str]:
        credentials = {'BEADS_DOLT_PASSWORD', 'BEADS_DOLT_CREDENTIAL_COMMAND',
                       'BEADS_DOLT_SERVER_USER', 'BEADS_DOLT_SERVER_TLS',
                       'BEADS_PROXIED_SERVER_EXTERNAL_PASSWORD'}
        environment = {key: value for key, value in os.environ.items()
                       if (key in credentials or not key.startswith(('BEADS_DOLT_', 'BEADS_PROXIED_SERVER_')))
                       and key not in {'BEADS_DIR', 'BEADS_DB', 'BEADS_MAX_ROWS', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'BEADS_SHARED_SERVER_DIR'}}
        environment['BEADS_DOLT_AUTO_START'] = '0'
        return environment

    @staticmethod
    async def _stop(process) -> None:
        if process.returncode is not None:
            await process.wait()
            return
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), .25)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    @staticmethod
    async def _finish(task):
        # Cancellation must not abandon a spawned child or interrupt its reaping.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        return await task

    async def _run(self, path: Path, *arguments: str):
        self._check_path(path)
        try:
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                'bd', '--readonly', '--sandbox', '--json', *arguments,
                cwd=path, env=self._environment(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                process = await self._finish(spawning)
                await self._finish(asyncio.create_task(self._stop(process)))
                raise
        except FileNotFoundError as error:
            raise BeadsError('bd is unavailable; install Beads and ensure bd is on PATH') from error
        except OSError as error:
            raise BeadsError(f'Cannot run bd in {path}: {error}') from error
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout)
        except (TimeoutError, asyncio.CancelledError) as error:
            await self._finish(asyncio.create_task(self._stop(process)))
            if isinstance(error, asyncio.CancelledError):
                raise
            raise BeadsError(f'bd {arguments[0]} timed out after {self.timeout:g}s') from error
        if process.returncode:
            message = stderr.decode(errors='replace').strip() or stdout.decode(errors='replace').strip()
            raise BeadsError(f'bd {arguments[0]} failed: {message or f"exit {process.returncode}"}')
        try:
            return json.loads(stdout)
        except (ValueError, UnicodeDecodeError) as error:
            raise BeadsError(f'bd {arguments[0]} returned invalid JSON') from error
