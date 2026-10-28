import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from beads_dashboard.beads import BeadsClient, BeadsError, Project


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name).resolve()
        (self.path / '.beads').mkdir()
        self.other_path = self.path / 'worktree'
        (self.other_path / '.beads').mkdir(parents=True)
        self.client = BeadsClient(timeout=1)
        self.project = Project('key', self.path, 'example', 'main')
        validation = patch.object(self.client, '_validate', new=AsyncMock(), create=True)
        validation.start()
        self.addCleanup(validation.stop)

    def process(self, value, code=0, stderr=b''):
        process = AsyncMock()
        process.returncode = code
        process.communicate.return_value = (json.dumps(value).encode(), stderr)
        return process

    def context(self, **overrides):
        return dict(backend='dolt', dolt_mode='embedded', beads_dir='/tmp/example/.beads',
                    repo_root='/tmp/example', database='example', project_id='uuid', **overrides)

    async def discover(self, context=None, branch='main', path=None):
        with patch('asyncio.create_subprocess_exec', side_effect=[
            self.process(context or self.context()), self.process({'current': branch, 'branches': [branch]})
        ]) as spawn:
            project = await self.client.project(path or self.path)
        return project, spawn

    async def test_project_uses_effective_data_and_current_dolt_branch(self):
        project, spawn = await self.discover()
        self.assertEqual((project.name, project.branch), ('example', 'main'))
        self.assertEqual(project.path, self.path)
        self.assertIn('branch', spawn.call_args.args)
        for call in spawn.call_args_list:
            self.assertTrue({'--readonly', '--sandbox', '--json'} <= set(call.args))
            self.assertEqual(call.kwargs['cwd'], self.path)

    async def test_same_database_from_worktree_dedupes(self):
        first, _ = await self.discover()
        context = self.context(); context.update(is_worktree=True, cwd_repo_root='/tmp/worktree')
        second, _ = await self.discover(context, path=self.other_path)
        self.assertEqual(first.key, second.key)

    async def test_distinct_physical_databases_and_branches_stay_distinct(self):
        first, _ = await self.discover()
        context = self.context(); context['beads_dir'] = '/tmp/clone/.beads'
        clone, _ = await self.discover(context)
        branch, _ = await self.discover(branch='feature')
        self.assertNotEqual(first.key, clone.key)
        self.assertNotEqual(first.key, branch.key)

    async def test_recreated_embedded_database_gets_new_identity(self):
        first, _ = await self.discover()
        context = self.context(); context['project_id'] = 'replacement'
        replacement, _ = await self.discover(context)
        self.assertNotEqual(first.key, replacement.key)

    async def test_malformed_context_fields_are_actionable(self):
        for key, value in [('beads_dir', 123), ('database', []), ('repo_root', 123), ('project_id', None)]:
            with self.subTest(key=key):
                context = self.context(); context[key] = value
                with patch('asyncio.create_subprocess_exec', side_effect=[self.process(context), self.process({'current': 'main'})]):
                    with self.assertRaises(BeadsError):
                        await self.client.project(self.path)

    async def test_cancellation_during_process_spawn_reaps_process(self):
        original_spawn = asyncio.create_subprocess_exec
        spawned = asyncio.Event()
        process = None
        async def slow_spawn(*args, **kwargs):
            nonlocal process
            process = await original_spawn(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
            spawned.set()
            await asyncio.sleep(.05)
            return process
        with patch('asyncio.create_subprocess_exec', side_effect=slow_spawn):
            task = asyncio.create_task(self.client.detail(self.project, 'x-1'))
            await spawned.wait()
            task.cancel()
            await asyncio.sleep(.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        try:
            self.assertIsNotNone(process.returncode)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def test_server_identity_includes_endpoint_database_and_branch(self):
        context = self.context(); context.update(dolt_mode='server', server_host='localhost', server_port=3307)
        first, _ = await self.discover(context)
        context['beads_dir'] = '/tmp/other/.beads'; context['project_id'] = 'other'
        same, _ = await self.discover(context)
        self.assertEqual(first.key, same.key)
        context['database'] = 'different'
        different, _ = await self.discover(context)
        self.assertNotEqual(first.key, different.key)

    async def test_incomplete_identity_is_reported(self):
        with patch('asyncio.create_subprocess_exec', return_value=self.process({'backend': 'dolt'})):
            with self.assertRaisesRegex(BeadsError, 'context|identity'):
                await self.client.project(self.path)

    async def test_proxy_mode_cannot_start_a_server(self):
        context = self.context(); context['dolt_mode'] = 'proxied-server'
        with patch('asyncio.create_subprocess_exec', return_value=self.process(context)) as spawn:
            with self.assertRaisesRegex(BeadsError, 'must not start a server'):
                await self.client.project(self.path)
        self.assertEqual(spawn.call_count, 1)

    async def test_missing_current_branch_is_reported(self):
        with patch('asyncio.create_subprocess_exec', side_effect=[self.process(self.context()), self.process({})]):
            with self.assertRaisesRegex(BeadsError, 'branch'):
                await self.client.project(self.path)

    async def test_snapshot_is_unlimited_and_preserves_native_fields(self):
        issues = [dict(id=f'x-{i}', description='body', labels=['label'], status='open') for i in range(65)]
        issues[-1]['status'] = 'closed'
        with patch('asyncio.create_subprocess_exec', side_effect=[self.process(issues), self.process([{'id': 'x-0'}]), self.process([{'id': 'x-1'}]), self.process({'current': 'main'})]) as spawn:
            result = await self.client.snapshot(self.project)
        self.assertEqual(len(result), 65)
        self.assertEqual(result[0]['description'], 'body')
        self.assertEqual(result[0]['labels'], ['label'])
        self.assertTrue(result[0]['_ready']); self.assertFalse(result[0]['_blocked'])
        self.assertFalse(result[1]['_ready']); self.assertTrue(result[1]['_blocked'])
        self.assertFalse(result[-1]['_ready'])
        for call in spawn.call_args_list[:2]:
            args = call.args
            for flag in ('--all', '--include-ephemeral', '--include-gates', '--include-infra', '--include-templates'):
                self.assertIn(flag, args)
            self.assertEqual(args[args.index('--limit') + 1], '0')
        self.assertIn('--ready', spawn.call_args_list[1].args)
        self.assertIn('blocked', spawn.call_args_list[2].args)

    async def test_missing_local_beads_never_falls_back_to_ancestor(self):
        (self.path / '.beads').rmdir()
        with patch('asyncio.create_subprocess_exec') as spawn:
            for operation in (self.client.project(self.path), self.client.snapshot(self.project), self.client.detail(self.project, 'x-1')):
                with self.assertRaisesRegex(BeadsError, '.beads'):
                    await operation
            spawn.assert_not_called()

    async def test_changed_identity_refuses_poll_and_detail(self):
        client = BeadsClient()
        changed = Project('new-database', self.path, 'replacement', 'feature')
        with patch.object(client, '_project', new=AsyncMock(return_value=changed), create=True), patch('asyncio.create_subprocess_exec') as spawn:
            for operation in (client.snapshot(self.project), client.detail(self.project, 'x-1')):
                with self.assertRaisesRegex(BeadsError, 'changed|identity'):
                    await operation
            spawn.assert_not_called()

    async def test_branch_change_during_snapshot_discards_results(self):
        with patch('asyncio.create_subprocess_exec', side_effect=[self.process([]), self.process([]), self.process([]), self.process({'current': 'other'})]):
            with self.assertRaisesRegex(BeadsError, 'branch.*changed|identity.*changed'):
                await self.client.snapshot(self.project)

    async def test_branch_change_during_detail_discards_other_branch_issue(self):
        detail = {'id': 'x-1', 'title': 'OTHER branch issue'}
        with patch('asyncio.create_subprocess_exec', side_effect=[self.process([detail]), self.process({'current': 'other'})]):
            with self.assertRaisesRegex(BeadsError, 'branch.*changed|identity.*changed'):
                await self.client.detail(self.project, 'x-1')

    async def test_null_lists_mean_empty_database(self):
        with patch('asyncio.create_subprocess_exec', side_effect=[*[self.process(None) for _ in range(3)], self.process({'current': 'main'})]):
            self.assertEqual(await self.client.snapshot(self.project), [])

    async def test_malformed_issue_shape_is_reported(self):
        for payload in ({'error': 'bad'}, [1], [{'title': 'no identity'}]):
            with self.subTest(payload=payload), patch('asyncio.create_subprocess_exec', return_value=self.process(payload)):
                with self.assertRaisesRegex(BeadsError, 'issue|JSON'):
                    await self.client.snapshot(self.project)

    async def test_detail_preserves_notes_comments_dependencies_and_missing_fields(self):
        detail = {'id': 'x-1', 'notes': 'note', 'comments': [{'text': 'comment'}], 'dependencies': [{'id': 'x-2'}]}
        with patch('asyncio.create_subprocess_exec', side_effect=[self.process([detail]), self.process({'current': 'main'})]) as spawn:
            result = await self.client.detail(self.project, 'x-1')
        self.assertEqual(result, detail)
        self.assertNotIn('design', result)
        args = spawn.call_args_list[0].args
        for flag in ('--long', '--include-comments', '--include-dependents', '--id=x-1'):
            self.assertIn(flag, args)

    async def test_missing_detail_is_reported(self):
        with patch('asyncio.create_subprocess_exec', side_effect=[self.process([]), self.process({'current': 'main'})]):
            with self.assertRaisesRegex(BeadsError, 'x-1'):
                await self.client.detail(self.project, 'x-1')

    async def test_missing_executable_is_actionable(self):
        with patch('asyncio.create_subprocess_exec', side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(BeadsError, 'bd.*install|Install.*bd'):
                await self.client.detail(self.project, 'x-1')

    async def test_invalid_json_and_nonzero_exit_are_actionable(self):
        process = self.process(None); process.communicate.return_value = (b'not json', b'')
        with patch('asyncio.create_subprocess_exec', return_value=process):
            with self.assertRaisesRegex(BeadsError, 'JSON'):
                await self.client.detail(self.project, 'x-1')
        with patch('asyncio.create_subprocess_exec', return_value=self.process(None, 1, b'database locked')):
            with self.assertRaisesRegex(BeadsError, 'database locked'):
                await self.client.detail(self.project, 'x-1')

    async def test_environment_uses_cwd_and_disables_server_start(self):
        ambient = {'BEADS_DIR': '/wrong', 'BEADS_DB': 'wrong', 'BEADS_DOLT_SERVER_DATABASE': 'wrong',
                   'BEADS_DOLT_SERVER_HOST': 'wrong', 'BEADS_DOLT_PASSWORD': 'secret',
                   'BEADS_PROXIED_SERVER_ROOT_PATH': '/wrong', 'BEADS_MAX_ROWS': '1', 'GIT_DIR': '/wrong', 'GIT_WORK_TREE': '/wrong'}
        with patch.dict(os.environ, ambient), patch('asyncio.create_subprocess_exec', side_effect=[self.process([{'id': 'x-1'}]), self.process({'current': 'main'})]) as spawn:
            await self.client.detail(self.project, 'x-1')
        env = spawn.call_args.kwargs['env']
        for key in ambient.keys() - {'BEADS_DOLT_PASSWORD'}:
            self.assertNotIn(key, env)
        self.assertEqual(env['BEADS_DOLT_PASSWORD'], 'secret')
        self.assertEqual(env['BEADS_DOLT_AUTO_START'], '0')

    async def test_snapshot_and_detail_for_same_key_do_not_overlap(self):
        active = 0
        async def spawn(*args, **kwargs):
            nonlocal active
            self.assertEqual(active, 0)
            active += 1
            process = self.process([{'id': 'x-1'}])
            async def communicate():
                nonlocal active
                await asyncio.sleep(.005)
                active -= 1
                return json.dumps({'current': 'main'} if 'branch' in args else [{'id': 'x-1'}]).encode(), b''
            process.communicate.side_effect = communicate
            return process
        with patch('asyncio.create_subprocess_exec', side_effect=spawn):
            await asyncio.gather(self.client.snapshot(self.project), self.client.detail(self.project, 'x-1'))

    async def exercise_stubborn_process(self, cancel):
        original_spawn = asyncio.create_subprocess_exec
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / 'pid'
            source = 'import os,signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(60)' % str(marker)
            async def spawn(*args, **kwargs):
                return await original_spawn(sys.executable, '-c', source, **kwargs)
            client = BeadsClient(timeout=.15)
            with patch('asyncio.create_subprocess_exec', side_effect=spawn):
                task = asyncio.create_task(client.detail(self.project, 'x-1'))
                if cancel:
                    while not marker.exists():
                        await asyncio.sleep(.005)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    with self.assertRaisesRegex(BeadsError, 'timed out'):
                        await task
            self.assertTrue(marker.exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(int(marker.read_text()), 0)

    async def test_timeout_kills_and_reaps_stubborn_process(self):
        await self.exercise_stubborn_process(False)

    async def test_cancellation_kills_and_reaps_stubborn_process(self):
        await self.exercise_stubborn_process(True)
