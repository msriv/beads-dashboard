"""Find candidate Beads project directories without reading their databases."""

import os
from pathlib import Path


_PRUNED = {".git", ".beads", "node_modules", ".venv", "venv", "vendor", "target", "build", "dist", "__pycache__", ".cache", ".tox"}


def discover(roots: list[Path], projects: list[Path]) -> tuple[list[Path], list[str]]:
    found: set[Path] = set()
    diagnostics: list[str] = []

    def canonical(path: Path) -> Path:
        return path.expanduser().resolve()

    def has_beads(path: Path) -> bool:
        beads = path / ".beads"
        return beads.exists() or beads.is_symlink()

    for raw_root in roots:
        root = canonical(Path(raw_root))
        if not root.is_dir():
            diagnostics.append(f"Root is missing or not a directory: {root}")
            continue

        def walk_error(error: OSError) -> None:
            diagnostics.append(f"Cannot scan {error.filename or root}: {error.strerror or error}")

        for current, dirs, _ in os.walk(root, topdown=True, followlinks=False, onerror=walk_error):
            dirs[:] = sorted(name for name in dirs if name not in _PRUNED)
            current_path = Path(current)
            if has_beads(current_path):
                found.add(current_path.resolve())

    for raw_project in projects:
        project = canonical(Path(raw_project))
        if not project.is_dir():
            diagnostics.append(f"Explicit project is missing or not a directory: {project}")
        elif has_beads(project):
            found.add(project)
        else:
            diagnostics.append(f"Explicit project is not initialized (missing .beads): {project}")

    return sorted(found), sorted(diagnostics)
