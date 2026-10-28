"""Command line entry point for the Beads dashboard."""

import argparse
import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path

from . import __version__


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="beads-dashboard",
        epilog="Without CLI targets or a config file, scans ~/Developer and ~/Documents/side-projects.",
    )
    result.add_argument("--root", action="append", type=Path, default=[],
                        help="scan a directory recursively; repeatable and overrides config/default targets")
    result.add_argument("--project", action="append", type=Path, default=[],
                        help="watch an explicit project directory; repeatable and overrides config/default targets")
    result.add_argument("--config", type=Path,
                        help="read roots/projects from TOML (default: $XDG_CONFIG_HOME/beads-dashboard/config.toml or ~/.config/beads-dashboard/config.toml)")
    result.add_argument("--once", action="store_true", help="print one JSON snapshot and exit")
    result.add_argument("--version", action="version", version=__version__)
    return result


def config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".config") / "beads-dashboard" / "config.toml"


def _paths(values: object, key: str, base: Path) -> list[Path]:
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"Config key '{key}' must be a list of strings")
    return [(path if path.is_absolute() else base / path).resolve() for value in values for path in [Path(value).expanduser()]]


def load_config(path: Path, *, missing_ok: bool = False) -> tuple[list[Path], list[Path]]:
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        if missing_ok:
            return [], []
        raise ValueError(f"Config file does not exist: {path}")
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"Cannot read config file {path}: {error}") from error
    unknown = data.keys() - {"roots", "projects"}
    if unknown:
        raise ValueError(f"Unknown config key(s): {', '.join(sorted(unknown))}")
    return _paths(data.get("roots", []), "roots", path.parent), _paths(data.get("projects", []), "projects", path.parent)


def resolve_targets(args: argparse.Namespace) -> tuple[list[Path], list[Path]]:
    if args.root or args.project:
        return [path.expanduser().resolve() for path in args.root], [path.expanduser().resolve() for path in args.project]
    path = args.config.expanduser().resolve() if args.config else config_path()
    roots, projects = load_config(path, missing_ok=args.config is None)
    if not roots and not projects and args.config is None and not path.exists():
        home = Path.home()
        roots = [home / "Developer", home / "Documents" / "side-projects"]
    return roots, projects


def _snapshot(monitor) -> dict:
    projects = []
    for key, state in sorted(monitor.states.items()):
        project = state.project
        projects.append({
            "path": str(project.path), "key": key, "name": project.name, "branch": project.branch,
            "issues": list(state.issues.values()), "error": state.error,
            "last_success": state.last_success.isoformat() if state.last_success else None,
            "duration": state.duration,
        })
    diagnostics = list(monitor.diagnostics)
    if not projects and not diagnostics:
        diagnostics.append("No Beads projects found.")
    return {"projects": projects, "diagnostics": diagnostics}


def _once(monitor, stdout) -> int:
    asyncio.run(monitor.once())
    snapshot = _snapshot(monitor)
    print(json.dumps(snapshot, sort_keys=True), file=stdout)
    return int(bool(monitor.diagnostics) or any(state.error for state in monitor.states.values()))


def main(argv: list[str] | None = None, *, stdout=None) -> int:
    output = stdout if stdout is not None else sys.stdout
    cli_parser = parser()
    args = cli_parser.parse_args(argv)
    try:
        roots, projects = resolve_targets(args)
    except ValueError as error:
        cli_parser.error(str(error))
    from .monitor import Monitor
    monitor = Monitor(roots=roots, projects=projects)
    try:
        if args.once:
            try:
                return _once(monitor, output)
            except KeyboardInterrupt:
                # asyncio.run cancels the one-shot coroutine during shutdown.
                return 130
        from .app import DashboardApp
        DashboardApp(monitor).run()
    except KeyboardInterrupt:
        # Textual's unmount hook awaits monitor.stop() during app teardown.
        return 130
    return 0
