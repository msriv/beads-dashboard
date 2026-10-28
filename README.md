# Beads dashboard

A keyboard-operated terminal dashboard for monitoring agent work across local Beads projects. Each project's tickets stay in its own database. The dashboard reads supported `bd` JSON commands and never edits tickets or project configuration.

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), and [Beads](https://github.com/steveyegge/beads) (`bd`) on `PATH`. Tested with Beads 1.3.2 and embedded Dolt on macOS. Textual supplies the terminal interface.

Install the tagged release from GitHub (requires Git):

```sh
uv tool install --force 'git+https://github.com/msriv/beads-dashboard.git@v0.1.1'
beads-dashboard
```

On macOS, install uv first with `brew install uv` if needed. If the installed command is not found, run `uv tool update-shell`, then open a new terminal. Check Beads with `bd --version`; install it using its upstream instructions if missing. The tagged-source command was verified with a fresh online installation.

Alternatively, install the packaged wheel without Git. Wheel, source archive, and checksums are available on the [v0.1.1 release page](https://github.com/msriv/beads-dashboard/releases/tag/v0.1.1):

```sh
uv tool install --force https://github.com/msriv/beads-dashboard/releases/download/v0.1.1/beads_dashboard-0.1.1-py3-none-any.whl
```

If uv stalls on GitHub's wheel download redirect, use the tagged-source command above. Both installs provide the `beads-dashboard` command from any folder; `--force` replaces an existing installation. To remove it, run `uv tool uninstall beads-dashboard`.

## Development

For a local checkout, dependencies are locked in `uv.lock`:

```sh
git clone https://github.com/msriv/beads-dashboard.git
cd beads-dashboard
uv sync --locked
uv run beads-dashboard
```

Use `uv tool install --editable .` for a command that reflects local source edits without reinstalling.

Without configuration, the dashboard searches `~/Developer` and `~/Documents/side-projects`. Select **All projects** or a single source using the project selector. Project numbers distinguish identical project names and ticket IDs; full paths appear in the selector, details, and health pane.

## Choose projects

`--root` searches a parent folder recursively. `--project` selects an exact initialized workspace. Repeat either flag:

```sh
beads-dashboard --root ~/Developer --root ~/work --project /another/place/planning
```

Supplying either flag replaces the saved/default target list for that invocation. To save targets, create `~/.config/beads-dashboard/config.toml` (or `$XDG_CONFIG_HOME/beads-dashboard/config.toml`):

```toml
roots = ["~/Developer", "~/Documents/side-projects"]
projects = ["/another/place/planning"]
```

Use `--config /path/to/config.toml` for another config file. Relative paths in a config are relative to that file's directory. Empty target lists deliberately scan nothing. Missing or unreadable locations appear as diagnostics rather than disappearing silently.

Discovery skips VCS internals, database files, dependency trees, and common build directories. It does not follow nested directory symlinks; pass a symlinked project/root explicitly. An initialized local `.beads` directory or symlink is required. Worktrees sharing the same effective database and Dolt branch appear once. Independent databases and working sets remain separate; a copied database is not merged based on its project ID alone.

## Keyboard

Click a ticket row once to select it and show its details. Keyboard navigation remains available for every action.

| Keys | Action |
| --- | --- |
| `Tab` / `Shift+Tab` | Move between the project selector, search, summaries, ticket table, details, activity, and health |
| Arrows | Navigate the focused table/selector or scroll a pane |
| `j` / `k` | Move down/up outside text entry |
| `Enter` | Open the selected ticket's full details; apply filters when in the filter form |
| `Esc` | Cancel a dialog or return to the ticket table |
| `/` | Focus search; typing immediately filters tickets |
| `f` | Filter by status, assignee, and priority; blank means all |
| `?` | Open keyboard help |
| `q` | Quit outside text entry |
| `PageUp` / `PageDown`, `Home` / `End` | Scroll the focused pane |

In the project selector, press `Enter`, choose with arrows, and press `Enter` again. In filters, use `Tab` between fields; `Unassigned` selects tickets without an assignee, and priorities accept `1` or `P1`. Clear search/filter fields to show everything again. Printable shortcuts do not interrupt search, filter fields, or selector type-ahead.

Refresh preserves input, focus, filters, and the selected ticket where it still exists. Details include descriptions, notes, acceptance criteria, labels, comments with recorded authors, and dependencies. Descriptions and comments render Markdown headings, emphasis, lists, and fenced code; ticket and section headings use distinct styling and spacing. Small terminals stack the ticket and detail panes; use `Tab` and scrolling, or enlarge the window for a side-by-side view.

## What live means

Each project refreshes independently, targeting a two-second cycle without overlapping reads of the same database. New projects are discovered every 20 seconds. A slow project shows its own error and last successful refresh; other projects continue updating. Last-known data stays visible and explicitly stale during failures.

Complete reads use several CLI calls to include all issue types, closed tickets, correct Beads ready/blocked semantics, and working-data identity checks. On this machine a 65-ticket snapshot took about 3.2 seconds; two databases containing 55 tickets took about 4.8 seconds for initial discovery, snapshots, and a full detail read. Refresh can take longer than two seconds, especially while agents write or a detail request waits for database access. The UI remains responsive. Reads of changing data are eventually consistent, rather than one transaction across every project.

Activity is the last 200 **observed** ticket changes since launch. Startup establishes a baseline. Intermediate edits between polls can be combined, and activity resets when the dashboard exits. Assignees and recorded update times describe Beads data; they do not prove that an agent is alive or identify who made an edit. Agents must record progress in Beads for it to appear here.

Embedded Dolt is verified. Existing external-server mode has identity/error handling but has not been exercised against a live server. Proxied-server mode is rejected because monitoring must not start infrastructure. Other Beads versions/backends may report an unsupported-mode error. Commands use `--readonly --sandbox`, isolate ambient workspace overrides, disable automatic server startup, and cancel/reap child processes on timeout or exit.

## One-shot output and verification

Print a single read-only JSON snapshot without starting a terminal UI:

```sh
beads-dashboard --project /path/to/project --once
```

Exit code is 1 for scan/read errors and 0 for a valid snapshot, including an empty result with a visible diagnostic. Each project includes its source identity, path, native ticket fields, last-success time, and any error. `_ready` and `_blocked` are dashboard annotations from Beads' own queries.

```sh
uv run python -m unittest discover -s tests -v
BD_DASHBOARD_INTEGRATION=1 uv run python -m unittest discover -s tests -p test_integration.py -v
uv build
```

The opt-in integration check creates and removes disposable Git/Beads projects. It verifies more than 50 tickets, independent databases with overlapping ticket IDs, comments, dependencies, assignments, blocking/unblocking, closing, live concurrent reads/writes, shared worktrees, unavailable-workspace recovery, and identical database/config file hashes before and after monitoring. It never writes to your real tracked projects.
