import asyncio
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from rich.console import Console
from rich.markdown import Markdown
from textual.widgets import DataTable, Input, Select, Static

try:
    from beads_dashboard.app import DashboardApp
except ImportError:
    DashboardApp = None


def detail_rendered(app):
    content = app.query_one('#detail-text', Static).content
    if isinstance(content, str):
        return content
    return ''.join(segment.text for segment in Console(width=100).render(content))


def project_state(key, name, issues):
    return SimpleNamespace(project=SimpleNamespace(key=key, name=name, path=Path('/projects') / key, branch='main'),
                           issues={issue['id']: issue for issue in issues}, error=None,
                           last_success=datetime.now(timezone.utc), refreshing=False, duration=0.02)


def issue(ticket='bd-1', **values):
    return {'id': ticket, 'title': 'A literal [red]ticket[/red]', 'status': 'open',
            'priority': 1, 'assignee': '', '_ready': True, '_blocked': False, **values}


class FakeMonitor:
    def __init__(self, states=()):
        self.states = {state.project.key: state for state in states}
        self.diagnostics = []
        self.changes = deque()
        self.on_update = None
        self.client = self
        self.stopped = False
        self.detail_error = None
        self.detail_calls = 0
        self.detail_values = {}

    async def run(self):
        await asyncio.Event().wait()

    async def stop(self):
        self.stopped = True

    async def detail(self, project, ticket):
        self.detail_calls += 1
        if self.detail_error:
            raise RuntimeError(self.detail_error)
        return self.detail_values or self.states[project.key].issues[ticket]


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def app(self, states=None):
        self.assertIsNotNone(DashboardApp, 'DashboardApp must provide the keyboard dashboard')
        self.monitor = FakeMonitor(states if states is not None else [project_state('one', 'Same', [issue(), issue('bd-2', title='Second', assignee='Ada', status='in_progress')])])
        return DashboardApp(self.monitor)

    async def test_single_mouse_click_opens_exact_ticket_and_survives_filtered_refresh(self):
        app = self.app([project_state('one', 'Same', [issue()]),
                        project_state('two', 'Same', [issue(), issue('bd-2', status='closed')])])
        async with app.run_test(size=(120, 40)) as pilot:
            table = app.query_one('#issues', DataTable)
            self.assertTrue(await pilot.click(table, offset=(2, 2)))
            await pilot.pause()
            self.assertEqual(app.selected_identity, ('two', 'bd-1'))
            self.assertEqual(app._detail_identity, ('two', 'bd-1'))
            self.assertIn('/projects/two', detail_rendered(app))
            self.assertIs(app.focused, app.query_one('#detail'))
            calls = self.monitor.detail_calls
            await pilot.click(table, offset=(2, 2))
            await pilot.pause()
            self.assertEqual(self.monitor.detail_calls, calls + 1)
            app.filters = ('closed', '', '')
            app.refresh_view()
            await pilot.pause()
            await pilot.click(table, offset=(2, 1))
            await pilot.pause()
            self.assertEqual(app.selected_identity, ('two', 'bd-2'))
            self.assertEqual(app._detail_identity, ('two', 'bd-2'))
            self.assertIn('bd-2', detail_rendered(app))
            self.monitor.states['two'].issues['bd-0'] = issue('bd-0', status='closed')
            self.monitor.on_update()
            await pilot.pause()
            self.assertEqual(app.selected_identity, ('two', 'bd-2'))
            self.assertEqual(table.cursor_row, 1)
            self.assertEqual(app._detail_identity, ('two', 'bd-2'))
            calls = self.monitor.detail_calls
            await pilot.click(table, offset=(2, 0))
            await pilot.click(table, offset=(2, 6))
            await pilot.pause()
            self.assertEqual(self.monitor.detail_calls, calls)
            self.assertEqual(app.selected_identity, ('two', 'bd-2'))

    async def test_stale_row_selection_after_rebuild_does_not_open_another_ticket(self):
        app = self.app()
        async with app.run_test() as pilot:
            table = app.query_one('#issues', DataTable)
            removed_key = table.ordered_rows[1].key
            del self.monitor.states['one'].issues['bd-2']
            self.monitor.on_update()
            await pilot.pause()
            table.post_message(DataTable.RowSelected(table, 1, removed_key))
            await pilot.pause()
            self.assertEqual(app.selected_identity, ('one', 'bd-1'))
            self.assertEqual(self.monitor.detail_calls, 0)

    async def test_detail_renders_markdown_styles_and_keeps_metadata_literal(self):
        app = self.app()
        self.monitor.detail_values = issue(
            title='Title **literal** [bold]tags[/bold] &copy;',
            assignee='**Ada**', labels=['*label*'],
            description='## Goal\n\n**Strong text** and [bold]literal[/bold]\n\n- First item\n- Second item\n\n```python\nprint(42)\n```\n\n[Reference](https://example.com)\n\n<script>not executed</script>',
            notes='**Note emphasis**',
            comments=[{'author': '*Bea*', 'text': '**Comment emphasis**'}],
            dependencies=[{'id': 'dep_1', 'title': '**dependency**'}])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press('enter')
            await pilot.pause()
            content = app.query_one('#detail-text', Static).content
            self.assertIsInstance(content, Markdown)
            segments = list(Console(width=100).render(content))
            text = ''.join(segment.text for segment in segments)
            for literal in ('Title **literal** [bold]tags[/bold] &copy;', '**Ada**', '*label*', '*Bea*',
                            '**dependency**', 'dep_1', '[bold]literal[/bold]'):
                self.assertIn(literal, text)
            for emphasized in ('Strong text', 'Note emphasis', 'Comment emphasis'):
                self.assertTrue(any(emphasized in segment.text and segment.style.bold for segment in segments))
            self.assertTrue(any('Description' in segment.text and segment.style and
                                (segment.style.bold or segment.style.underline or segment.style.color) for segment in segments))
            self.assertTrue(any('•' in segment.text for segment in segments))
            self.assertTrue(any('print' in segment.text and segment.style and segment.style.bgcolor for segment in segments))
            self.assertIn('Reference (https://example.com)', text)
            self.assertFalse(any(segment.style and segment.style.link for segment in segments))

    async def test_search_printable_bindings_and_escape(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press('/')
            await pilot.press('q', 'f', 'j', 'k', 'question_mark', 'slash')
            self.assertEqual(app.query_one('#search', Input).value, 'qfjk?/')
            self.assertIs(app.focused, app.query_one('#search'))
            self.assertEqual(len(app.screen_stack), 1)
            await pilot.press('escape')
            self.assertIs(app.focused, app.query_one('#issues'))
        self.assertTrue(self.monitor.stopped)

    async def test_sources_selection_and_update_preserve_search_focus(self):
        app = self.app([project_state('one', 'Same', [issue()]), project_state('two', 'Same', [issue()])])
        async with app.run_test() as pilot:
            table = app.query_one('#issues', DataTable)
            self.assertEqual(table.row_count, 2)
            self.assertEqual(table.get_row_at(0)[0].plain, '1 Same')
            self.assertEqual(table.get_row_at(1)[0].plain, '2 Same')
            await pilot.press('j')
            self.assertEqual(app.selected_identity, ('two', 'bd-1'))
            await pilot.press('/')
            await pilot.press('t', 'i', 'c', 'k', 'e', 't')
            self.monitor.states['one'].issues['bd-0'] = issue('bd-0')
            self.monitor.on_update()
            await pilot.pause()
            self.assertEqual(app.selected_identity, ('two', 'bd-1'))
            self.assertEqual(app.query_one('#search', Input).value, 'ticket')
            self.assertIs(app.focused, app.query_one('#search'))
            self.assertEqual(table.row_count, 3)

    async def test_keyboard_filters_search_and_project_selection(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press('f')
            self.assertEqual(len(app.screen_stack), 2)
            await pilot.press('i', 'n', '_', 'p', 'r', 'o', 'g', 'r', 'e', 's', 's', 'tab')
            await pilot.press('A', 'd', 'a', 'tab', '1', 'enter')
            await pilot.pause()
            self.assertEqual(app.query_one('#issues', DataTable).row_count, 1)
            self.assertEqual(app.selected_identity, ('one', 'bd-2'))
            await pilot.press('/')
            await pilot.press('x')
            self.assertEqual(app.query_one('#issues', DataTable).row_count, 0)
            self.assertIsNone(app.selected_identity)
            await pilot.press('escape')
            app.query_one('#projects', Select).value = 'one'
            await pilot.pause()
            self.assertEqual(app.query_one('#projects', Select).value, 'one')

    async def test_full_detail_literal_text_error_and_scroll(self):
        app = self.app()
        self.monitor.detail_values = issue(description='Long [bold]description[/bold]\n' * 70,
            notes='Notes', acceptance_criteria='Acceptance', labels=['a'], comments=[{'author': 'Bea', 'text': 'Comment'}],
            dependencies=[{'id': 'dep-1'}])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press('enter')
            await pilot.pause()
            detail = detail_rendered(app)
            for expected in ('Description', 'Long [bold]description[/bold]', 'Notes', 'Acceptance criteria', 'Acceptance', 'Comments', 'Bea', 'Dependencies', 'dep-1', '/projects/one'):
                self.assertIn(expected, detail)
            self.assertIs(app.focused, app.query_one('#detail'))
            await pilot.press('j', 'j', 'pagedown')
            self.assertGreater(app.query_one('#detail').scroll_y, 0)
            await pilot.press('escape')
            self.monitor.detail_error = 'database unavailable'
            await pilot.press('enter')
            await pilot.pause()
            self.assertIn('database unavailable', detail_rendered(app))

    async def test_summary_health_activity_empty_resize_and_help(self):
        app = self.app([project_state('one', 'One', [issue(), issue('bd-2', status='in_progress', _ready=False), issue('bd-3', _ready=False, _blocked=True), issue('bd-4', status='closed', _ready=False)])])
        self.monitor.states['one'].error = 'Cannot poll'
        self.monitor.diagnostics = ['Missing /projects/gone']
        self.monitor.changes.append(SimpleNamespace(at=datetime.now(timezone.utc), project_key='one', project_name='One', issue_id='bd-2', title='Changed', kind='updated', fields=('status',)))
        async with app.run_test(size=(120, 40)) as pilot:
            summary = str(app.query_one('#summary', Static).render())
            for label in ('Ready 1', 'In progress 1', 'Blocked 1', 'Closed 1', 'Unassigned'):
                self.assertIn(label, summary)
            health = str(app.query_one('#health-text', Static).render())
            self.assertIn('Cannot poll', health)
            self.assertIn('Missing /projects/gone', health)
            self.assertIn('last success', health)
            self.assertIn('bd-2', str(app.query_one('#activity-text', Static).render()))
            await pilot.press('question_mark')
            self.assertEqual(len(app.screen_stack), 2)
            await pilot.press('escape')
            await pilot.resize_terminal(45, 16)
            await pilot.pause()
            self.monitor.states.clear()
            self.monitor.on_update()
            await pilot.pause()
            self.assertEqual(app.query_one('#issues', DataTable).row_count, 0)
            self.assertIn('No projects', str(app.query_one('#health-text', Static).render()))

    async def test_removed_selection_falls_back_without_stale_detail(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press('j', 'enter')
            await pilot.pause()
            del self.monitor.states['one'].issues['bd-2']
            self.monitor.on_update()
            await pilot.pause()
            self.assertEqual(app.selected_identity, ('one', 'bd-1'))
            self.assertNotIn('Second', detail_rendered(app))

    async def test_background_update_preserves_filter_draft(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press('f', 'q', 'j')
            self.monitor.states['one'].issues['bd-3'] = issue('bd-3')
            self.monitor.on_update()
            await pilot.pause()
            self.assertEqual(app.screen.query_one('#filter-status', Input).value, 'qj')
            self.assertEqual(len(app.screen_stack), 2)
            await pilot.press('escape')
            self.assertEqual(app.query_one('#issues', DataTable).row_count, 3)

    async def test_detail_refreshes_only_changed_selected_snapshot_and_keeps_scroll(self):
        app = self.app()
        self.monitor.detail_values = issue(description='Long detail\n' * 80)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press('enter')
            await pilot.pause()
            await pilot.press('pagedown')
            await pilot.pause()
            position = app.query_one('#detail').scroll_y
            self.assertGreater(position, 0)
            self.monitor.on_update()
            await pilot.pause()
            self.assertEqual(self.monitor.detail_calls, 1)
            self.monitor.states['one'].issues['bd-1']['title'] = 'Live update'
            self.monitor.detail_values['title'] = 'Live update'
            self.monitor.on_update()
            await pilot.pause()
            self.assertEqual(self.monitor.detail_calls, 2)
            self.assertIn('Live update', detail_rendered(app))
            self.assertEqual(app.query_one('#detail').scroll_y, position)

    async def test_tab_shift_tab_and_keyboard_project_selector(self):
        app = self.app([project_state('one', 'One', [issue()]), project_state('two', 'Two', [issue()])])
        async with app.run_test() as pilot:
            table = app.query_one('#issues')
            await pilot.press('tab')
            self.assertIs(app.focused, app.query_one('#detail'))
            await pilot.press('shift+tab')
            self.assertIs(app.focused, table)
            await pilot.press('shift+tab', 'shift+tab', 'shift+tab', 'enter', 'down', 'enter')
            await pilot.pause()
            self.assertEqual(app.query_one('#projects', Select).value, 'one')
            self.assertEqual(app.query_one('#issues', DataTable).row_count, 1)

    async def test_agent_groups_scroll_and_small_layout_keeps_health_visible(self):
        app = self.app([project_state('one', 'One', [issue(f'bd-{n}', assignee=f'Agent {n}') for n in range(30)])])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press('shift+tab')
            self.assertEqual(app.focused.id, 'summary-pane')
            await pilot.press('end')
            await pilot.pause()
            self.assertGreater(app.focused.scroll_y, 0)
            await pilot.resize_terminal(45, 16)
            await pilot.pause()
            self.assertLessEqual(app.query_one('#health').region.bottom, 15)

    async def test_expanded_project_selector_keeps_printable_shortcuts_as_typing(self):
        app = self.app()
        async with app.run_test() as pilot:
            app.query_one('#projects', Select).focus()
            await pilot.press('enter', 'f', 'j', 'q', 'question_mark', 'slash')
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertEqual(len(app.screen_stack), 1)
            self.assertTrue(app.query_one('#projects', Select).expanded)
            await pilot.press('escape')
            self.assertFalse(app.query_one('#projects', Select).expanded)

    async def test_summary_and_agents_follow_filters_and_search(self):
        app = self.app([project_state('one', 'One', [issue(), issue('bd-2', status='closed', _ready=False)])])
        async with app.run_test() as pilot:
            await pilot.press('f', 'c', 'l', 'o', 's', 'e', 'd', 'enter')
            await pilot.pause()
            self.assertEqual(app.query_one('#issues', DataTable).row_count, 1)
            summary = str(app.query_one('#summary', Static).render())
            for expected in ('Ready 0', 'Closed 1', 'Unassigned 1'):
                self.assertIn(expected, summary)
            await pilot.press('/', 'z', 'z', 'z')
            self.assertEqual(app.query_one('#issues', DataTable).row_count, 0)
            summary = str(app.query_one('#summary', Static).render())
            self.assertIn('Ready 0', summary)
            self.assertIn('Closed 0', summary)
            self.assertIn('Agents: none', summary)

    async def test_background_refresh_preserves_horizontal_table_scroll(self):
        app = self.app([project_state('one', 'One', [issue(title='Long ticket title ' * 20), issue('bd-2')])])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press('right', 'right', 'right')
            await pilot.pause()
            table = app.query_one('#issues', DataTable)
            position = table.scroll_x
            self.assertGreater(position, 0)
            selected = app.selected_identity
            self.monitor.states['one'].issues['bd-2']['status'] = 'in_progress'
            self.monitor.on_update()
            await pilot.pause()
            self.assertEqual(table.scroll_x, position)
            self.assertEqual(app.selected_identity, selected)
            self.assertIs(app.focused, table)

    async def test_search_burst_does_not_dispatch_printable_bindings(self):
        from textual.events import Key
        app = self.app()
        async with app.run_test() as pilot:
            for key, character in [('slash', '/'), ('q', 'q'), ('f', 'f'), ('j', 'j'), ('question_mark', '?')]:
                app.post_message(Key(key, character))
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.query_one('#search', Input).value, 'qfj?')
            self.assertIs(app.focused, app.query_one('#search'))

    async def test_escape_quit_burst_leaves_no_deferred_focus(self):
        from textual.events import Key
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press('/')
            app.post_message(Key('escape', None))
            app.post_message(Key('q', 'q'))
            await pilot.pause()
            self.assertFalse(app.is_running)

    async def test_filter_burst_does_not_quit_during_modal_mount(self):
        from textual.events import Key
        app = self.app()
        async with app.run_test() as pilot:
            app.post_message(Key('f', 'f'))
            app.post_message(Key('q', 'q'))
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertEqual(len(app.screen_stack), 2)
            self.assertIs(app.focused, app.screen.query_one('#filter-status', Input))
            self.assertEqual(app.screen.query_one('#filter-status', Input).value, 'q')
