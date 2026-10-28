"""Keyboard-operated, read-only view of the monitor's native Beads snapshots."""

from collections import Counter
from datetime import datetime, timezone
import json
import re
from typing import TYPE_CHECKING

from rich.markdown import Markdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Select, Static, TextArea

if TYPE_CHECKING:
    from .monitor import Monitor


def markdown_literal(value):
    return re.sub(r'([\\`*_{}\[\]<>()#+\-.!|=~&])', r'\\\1', str(value))


class TicketTable(DataTable):
    def on_click(self, event):
        row = event.style.meta.get('row', -1)
        column = event.style.meta.get('column', -1)
        if row < 0 or column < 0:
            return
        # A ticket opens on its first click; Textual otherwise requires a second click.
        event.prevent_default()
        event.stop()
        if event.style.meta.get('out_of_bounds') or row >= self.row_count:
            return
        self.move_cursor(row=row, column=column)
        self.post_message(self.RowSelected(self, row, self.ordered_rows[row].key))


class ScrollPane(VerticalScroll):
    can_focus = True


class Filters(ModalScreen):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = """
    Filters { align: center middle; }
    Filters > Vertical { width: 60; max-width: 95%; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    Filters Input { margin-bottom: 1; }
    """

    def __init__(self, values):
        super().__init__()
        self.values = values

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static('Filters — blank means all; Enter applies, Esc cancels')
            for name, value in zip(('status', 'assignee', 'priority'), self.values):
                yield Static(name.title() + (' (use Unassigned for nobody)' if name == 'assignee' else ''))
                yield Input(value, id='filter-' + name)
            yield Button('Apply', id='apply', variant='primary')

    def on_mount(self):
        self.set_focus(self.query_one(Input))

    @on(Input.Submitted)
    @on(Button.Pressed, '#apply')
    def apply(self):
        self.dismiss(tuple(widget.value.strip().casefold() for widget in self.query(Input)))

    def action_cancel(self):
        self.dismiss(None)


class Help(ModalScreen):
    BINDINGS = [('escape', 'close', 'Close'), ('question_mark', 'close', 'Close')]
    DEFAULT_CSS = """
    Help { align: center middle; }
    Help ScrollPane { width: 70; max-width: 95%; height: 24; max-height: 95%; border: round $accent; padding: 1 2; background: $surface; }
    """

    def compose(self) -> ComposeResult:
        with ScrollPane():
            yield Static('Keyboard controls\n\n'
                         'Tab / Shift+Tab: next / previous pane\n'
                         'Arrows: navigate tables and selectors, scroll panes\n'
                         'j / k: move down / up outside text entry\n'
                         'Enter: open selected ticket and focus details\n'
                         'PageUp / PageDown / Home / End: scroll focused pane\n'
                         '/: focus search (type to filter immediately)\n'
                         'f: status, assignee, priority filters\n'
                         'Esc: cancel dialog or return to ticket table\n'
                         '?: this help\nq: quit outside text entry\n\n'
                         'Project selector: Enter, arrows, Enter.\n'
                         'Filters: Tab between fields; Enter applies.\n'
                         'Clear search or filter fields to show all tickets.\n'
                         'Details include recorded actors; assignment is not an audit trail.\n'
                         'Activity shows observed changes since launch; startup is a baseline.', markup=False)

    def action_close(self):
        self.dismiss()


class DashboardApp(App):
    TITLE = 'Beads dashboard'
    BINDINGS = [
        Binding('/', 'search', 'Search', priority=True),
        Binding('f', 'filters', 'Filters', priority=True),
        Binding('j', 'move(1)', 'Down', show=False, priority=True),
        Binding('k', 'move(-1)', 'Up', show=False, priority=True),
        Binding('enter', 'details', 'Details'),
        Binding('escape', 'back', 'Back', priority=True),
        Binding('question_mark', 'help', 'Help', priority=True),
        Binding('q', 'quit', 'Quit', priority=True),
    ]
    CSS = """
    Screen { layout: vertical; }
    #projects, #search { height: 3; }
    #summary-pane { height: 3; }
    #summary { height: auto; padding: 0 1; }
    #body { height: 1fr; min-height: 4; }
    #issues { width: 3fr; height: 1fr; }
    #detail { width: 2fr; height: 1fr; border: round $accent; padding: 0 1; }
    #detail:focus, #activity:focus, #health:focus { border: round $warning; }
    #activity { height: 4; border: round $secondary; }
    #health { height: 4; border: round $secondary; }
    .compact #body { layout: vertical; }
    .compact #issues, .compact #detail { width: 1fr; height: 1fr; }
    .compact #summary-pane { height: 2; }
    .compact #activity, .compact #health { height: 3; }
    .tiny #summary-pane { height: 1; }
    .tiny #activity, .tiny #health { height: 2; border: none; }
    .tiny #body { min-height: 3; }
    """

    class Updated(Message):
        pass

    def __init__(self, monitor: 'Monitor'):
        super().__init__()
        self.monitor = monitor
        self.filters = ('', '', '')
        self.selected_identity = None
        self._rows = []
        self._project_options = None
        self._rebuilding = False
        self._detail_identity = None
        self._detail_version = None

    def compose(self) -> ComposeResult:
        yield Select([('All projects', '')], value='', allow_blank=False, id='projects')
        yield Input(placeholder='Search tickets (Esc returns to table)', id='search')
        with ScrollPane(id='summary-pane'):
            yield Static('', id='summary', markup=False)
        with Horizontal(id='body'):
            yield TicketTable(id='issues', cursor_type='row', zebra_stripes=True)
            with ScrollPane(id='detail'):
                yield Static('Select a ticket and press Enter for full detail.', id='detail-text', markup=False)
        with ScrollPane(id='activity'):
            yield Static('', id='activity-text', markup=False)
        with ScrollPane(id='health'):
            yield Static('', id='health-text', markup=False)
        yield Footer()

    def on_mount(self):
        self.query_one('#issues', DataTable).add_columns('Project', 'Ticket', 'Priority', 'Status', 'Assignee', 'Title', 'Age')
        self.monitor.on_update = lambda: self.post_message(self.Updated())
        self.refresh_view()
        self.screen.set_focus(self.query_one('#issues'))
        self.run_worker(self.monitor.run(), name='monitor', exit_on_error=False)

    async def on_unmount(self):
        self.monitor.on_update = None
        await self.monitor.stop()

    def on_resize(self, event):
        self.screen.set_class(event.size.width < 90 or event.size.height < 25, 'compact')
        self.screen.set_class(event.size.height < 22, 'tiny')
        if self._project_options is not None:
            self.refresh_view()

    def check_action(self, action, parameters):
        if action == 'back' and len(self.screen_stack) > 1:
            return False
        if action in {'search', 'filters', 'move', 'help', 'quit', 'details'}:
            focused = self.focused
            typing = focused is not None and any(isinstance(widget, (Input, TextArea, Select))
                                                  for widget in (focused, *focused.ancestors))
            if typing or (len(self.screen_stack) > 1 and action != 'quit'):
                return False
        return True

    @on(Updated)
    def monitor_updated(self):
        self.refresh_view()

    @on(Input.Changed, '#search')
    @on(Select.Changed, '#projects')
    def selection_changed(self):
        if self.is_mounted:
            self.refresh_view()

    @staticmethod
    def age(value):
        if not value:
            return '—'
        try:
            moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            minutes = max(0, int((datetime.now(timezone.utc) - moment).total_seconds() / 60))
            return f'{minutes}m' if minutes < 60 else f'{minutes // 60}h' if minutes < 1440 else f'{minutes // 1440}d'
        except (ValueError, TypeError):
            return str(value)

    def refresh_view(self):
        if self._rebuilding:
            return
        self._rebuilding = True
        try:
            projects = self.query_one('#projects', Select)
            options = tuple((key, state.project.name, str(state.project.path)) for key, state in sorted(self.monitor.states.items()))
            source_labels = {key: f'{number} {name}' for number, (key, name, _) in enumerate(options, 1)}
            if options != self._project_options:
                current = projects.value
                projects.set_options([(Text('All projects'), '')] + [(Text(f'{source_labels[key]} — {path}'), key) for key, name, path in options])
                projects.value = current if current in self.monitor.states else ''
                self._project_options = options
            scope = [(key, state) for key, state in sorted(self.monitor.states.items()) if not projects.value or key == projects.value]
            all_issues = [(key, state, issue) for key, state in scope for issue in sorted(state.issues.values(), key=lambda value: str(value['id']))]
            search = self.query_one('#search', Input).value.casefold()
            status, assignee, priority = self.filters
            visible = [(key, state, issue) for key, state, issue in all_issues
                       if (not search or search in ' '.join((str(state.project.path), state.project.name, json.dumps(issue, ensure_ascii=False, default=str))).casefold())
                       and (not status or str(issue.get('status', '')).casefold() == status)
                       and (not assignee or str(issue.get('assignee') or 'Unassigned').casefold() == assignee)
                       and (not priority or str(issue.get('priority', '')).casefold() == priority.removeprefix('p'))]
            agents = Counter(str(issue.get('assignee') or 'Unassigned') for _, _, issue in visible)
            summary = (f'Ready {sum(bool(i.get("_ready")) for _, _, i in visible)}  '
                       f'In progress {sum(i.get("status") == "in_progress" for _, _, i in visible)}  '
                       f'Blocked {sum(bool(i.get("_blocked")) for _, _, i in visible)}  '
                       f'Closed {sum(i.get("status") == "closed" for _, _, i in visible)}\n'
                       'Agents: ' + (', '.join(f'{agent} {count}' for agent, count in sorted(agents.items())) or 'none'))
            self.query_one('#summary', Static).update(summary)
            previous = self.selected_identity
            rows = [(key, str(issue['id'])) for key, _, issue in visible]
            same_rows = rows == self._rows
            self._rows = rows
            table = self.query_one('#issues', DataTable)
            if not same_rows:
                table.clear()
            for row, (key, state, issue) in enumerate(visible):
                status_value = str(issue.get('status', 'unknown'))
                status_style = 'green' if status_value == 'closed' else 'yellow' if issue.get('_blocked') else 'cyan'
                cells = (Text(source_labels[key]), Text(str(issue['id'])),
                              Text(f'P{issue.get("priority", "?")}'), Text(status_value + (' / blocked' if issue.get('_blocked') else ''), style=status_style),
                              Text(str(issue.get('assignee') or 'Unassigned')), Text(str(issue.get('title', ''))), Text(self.age(issue.get('updated_at'))))
                if same_rows:
                    for column, cell in enumerate(cells):
                        table.update_cell_at(Coordinate(row, column), cell, update_width=True)
                else:
                    table.add_row(*cells, key=json.dumps((key, str(issue['id']))))
            index = self._rows.index(previous) if previous in self._rows else 0
            self.selected_identity = self._rows[index] if self._rows else None
            if self._rows:
                table.move_cursor(row=index, animate=False, scroll=not same_rows)
            if self._detail_identity and self._detail_identity != self.selected_identity:
                self.workers.cancel_group(self, 'detail')
                self._detail_identity = None
                self.query_one('#detail-text', Static).update('Selection changed. Press Enter for full detail.' if self._rows else 'No matching tickets.')
            elif self._detail_identity:
                key, ticket = self._detail_identity
                version = json.dumps(self.monitor.states[key].issues[ticket], sort_keys=True, default=str)
                if version != self._detail_version:
                    self.load_detail(self._detail_identity, preserve_scroll=True)
            elif not self._rows:
                self.query_one('#detail-text', Static).update('No matching tickets. Clear search/filters, or wait for the first snapshot.')
            activity = ['Observed changes since launch (startup is baseline; not an audit log)']
            for change in reversed(self.monitor.changes):
                if not projects.value or change.project_key == projects.value:
                    activity.append(f'{change.at:%H:%M:%S} {source_labels.get(change.project_key, change.project_name)} {change.issue_id} {change.kind}: {change.title} [{", ".join(change.fields)}]')
            self.query_one('#activity-text', Static).update('\n'.join(activity) + ('\nNo observed changes.' if len(activity) == 1 else ''))
            health = []
            for _, state in scope:
                success = state.last_success.isoformat(timespec='seconds') if state.last_success else 'never'
                freshness = 'STALE / ERROR: ' + state.error if state.error else 'refreshing' if state.refreshing else 'waiting for first snapshot' if not state.last_success else 'OK'
                health.append(f'{state.project.name} — {state.project.path} ({state.project.branch or "no branch"}): {freshness}; last success {success}; {state.duration:.2f}s')
            health.extend(self.monitor.diagnostics)
            if not self.monitor.states:
                health.insert(0, 'No projects discovered yet. Waiting for scan; check configured roots/projects.')
            if self.filters != ('', '', ''):
                health.insert(0, f'Filters: status={status or "all"}, assignee={assignee or "all"}, priority={priority or "all"} — {len(visible)} visible')
            if self.size.width < 90 or self.size.height < 25:
                health.insert(0, 'Small terminal: Tab to panes, arrows/PageDown scroll; enlarge for side-by-side detail.')
            self.query_one('#health-text', Static).update('\n'.join(health))
        finally:
            self._rebuilding = False

    @on(DataTable.RowHighlighted, '#issues')
    def row_highlighted(self, event):
        table = self.query_one('#issues', DataTable)
        if self._rows and event.row_key == table.ordered_rows[table.cursor_row].key:
            self.selected_identity = self._rows[table.cursor_row]

    @on(DataTable.RowSelected, '#issues')
    def row_selected(self, event):
        table = self.query_one('#issues', DataTable)
        if event.row_key not in table.rows:
            return
        index = table.get_row_index(event.row_key)
        self.selected_identity = self._rows[index]
        table.move_cursor(row=index)
        self.action_details()

    def action_search(self):
        self.screen.set_focus(self.query_one('#search'))

    async def action_filters(self):
        def apply(values):
            if values is not None:
                self.filters = values
                self.refresh_view()
            self.screen.set_focus(self.query_one('#issues'))
        await self.push_screen(Filters(self.filters), apply)

    def action_move(self, direction):
        focused = self.focused
        if isinstance(focused, DataTable):
            focused.action_cursor_down() if direction > 0 else focused.action_cursor_up()
        elif isinstance(focused, VerticalScroll):
            focused.scroll_down() if direction > 0 else focused.scroll_up()

    def action_back(self):
        projects = self.query_one('#projects', Select)
        if projects.expanded:
            projects.expanded = False
        self.screen.set_focus(self.query_one('#issues'))

    def action_help(self):
        self.push_screen(Help())

    def action_details(self):
        if self.selected_identity:
            self.screen.set_focus(self.query_one('#detail'))
            self.load_detail(self.selected_identity)

    @work(exclusive=True, group='detail', exit_on_error=False)
    async def load_detail(self, identity, preserve_scroll=False):
        key, ticket = identity
        state = self.monitor.states.get(key)
        if state is None:
            return
        self._detail_identity = identity
        self._detail_version = json.dumps(state.issues[ticket], sort_keys=True, default=str)
        scroll_y = self.query_one('#detail').scroll_y if preserve_scroll else 0
        heading = f'{state.project.name} — {state.project.path}\n{ticket}\n'
        if not preserve_scroll:
            self.query_one('#detail-text', Static).update(heading + 'Loading full detail…')
        try:
            detail = await self.monitor.client.detail(state.project, ticket)
            content = Markdown(f'{markdown_literal(state.project.name)} — {markdown_literal(state.project.path)}\n\n'
                               f'**{markdown_literal(ticket)}**\n\n' + self.detail_text(detail), hyperlinks=False)
        except Exception as error:
            content = heading + '\nDetail unavailable: ' + str(error)
        if self.selected_identity == identity:
            self.query_one('#detail-text', Static).update(content)
            self.query_one('#detail').scroll_to(y=scroll_y, animate=False)

    @staticmethod
    def detail_text(detail):
        """Keep native fields visible without interpreting assignment as attribution."""
        def value_text(value):
            if isinstance(value, dict):
                return '\n\n'.join(f'{markdown_literal(key.replace("_", " ").capitalize())}: {value_text(item)}' for key, item in value.items())
            if isinstance(value, list):
                return '\n\n'.join(value_text(item) for item in value)
            return markdown_literal(value)

        sections = ['# ' + markdown_literal(detail.get('title', '(Untitled)')),
                    f'Status: {markdown_literal(detail.get("status", "unknown"))}   Priority: P{markdown_literal(detail.get("priority", "?"))}',
                    f'Assignee: {markdown_literal(detail.get("assignee") or "Unassigned")}',
                    'Recorded actors below come from Beads; assignment does not identify the last actor.']
        handled = {'id', 'title', 'status', 'priority', 'assignee', '_ready', '_blocked'}
        for key, label in [('description', 'Description'), ('notes', 'Notes'),
                           ('acceptance_criteria', 'Acceptance criteria'), ('acceptance', 'Acceptance'),
                           ('labels', 'Labels')]:
            handled.add(key)
            if detail.get(key):
                body = detail[key] if key != 'labels' and isinstance(detail[key], str) else value_text(detail[key])
                sections.append('## ' + label + '\n\n' + body)
        for key, label in [('comments', 'Comments'), ('dependencies', 'Dependencies'), ('dependents', 'Dependents')]:
            handled.add(key)
            records = detail.get(key)
            if not records:
                continue
            lines = []
            for record in records if isinstance(records, list) else [records]:
                if not isinstance(record, dict):
                    lines.append(markdown_literal(record))
                elif key == 'comments':
                    actor = record.get('author') or record.get('created_by') or 'Actor not recorded'
                    at = record.get('created_at') or record.get('timestamp') or ''
                    text = record.get('text') or record.get('body') or record.get('comment') or ''
                    lines.append(f'{markdown_literal(actor)}{" — " + markdown_literal(at) if at else ""}\n\n{text}')
                    extra = {field: value for field, value in record.items() if field not in {'author', 'created_by', 'created_at', 'timestamp', 'text', 'body', 'comment'}}
                    if extra:
                        lines.append(value_text(extra))
                else:
                    ticket = record.get('id') or record.get('depends_on_id') or record.get('issue_id') or ''
                    lines.append(markdown_literal(ticket) + '\n\n' + value_text({field: value for field, value in record.items() if field not in {'id', 'depends_on_id', 'issue_id'}}))
            sections.append('## ' + label + '\n\n' + '\n\n'.join(lines))
        sections.extend('## ' + markdown_literal(key.replace('_', ' ').capitalize()) + '\n\n' + value_text(value)
                        for key, value in detail.items() if key not in handled and not key.startswith('_') and value is not None)
        return '\n\n'.join(sections)
