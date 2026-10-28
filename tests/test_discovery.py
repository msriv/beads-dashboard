import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from beads_dashboard.discovery import discover


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_finds_nested_projects_and_prunes_non_project_trees(self):
        root = self.base / "workspace"
        for path in (root / "alpha" / ".beads", root / "group" / "beta" / ".beads"):
            path.mkdir(parents=True)
        (root / "vendor" / "hidden" / ".beads").mkdir(parents=True)
        (root / "alpha" / ".git" / "nested" / ".beads").mkdir(parents=True)

        found, diagnostics = discover([root], [])

        self.assertEqual(found, [(root / "alpha").resolve(), (root / "group" / "beta").resolve()])
        self.assertEqual(diagnostics, [])

    def test_explicit_paths_are_included_outside_roots_and_aliases_deduplicate(self):
        project = self.base / "outside" / "project"
        (project / ".beads").mkdir(parents=True)
        alias = self.base / "alias"
        alias.symlink_to(project, target_is_directory=True)

        found, diagnostics = discover([], [alias, project])

        self.assertEqual(found, [project.resolve()])
        self.assertEqual(diagnostics, [])

    def test_symlink_beads_and_symlink_root_are_supported_without_following_loops(self):
        actual = self.base / "actual-beads"
        actual.mkdir()
        root = self.base / "root"
        project = root / "project"
        project.mkdir(parents=True)
        (project / ".beads").symlink_to(actual, target_is_directory=True)
        (project / "loop").symlink_to(root, target_is_directory=True)
        root_alias = self.base / "root-alias"
        root_alias.symlink_to(root, target_is_directory=True)

        found, diagnostics = discover([root, root_alias], [])

        self.assertEqual(found, [project.resolve()])
        self.assertEqual(diagnostics, [])

    def test_explicit_incomplete_project_and_missing_paths_report_diagnostics(self):
        incomplete = self.base / "incomplete"
        incomplete.mkdir()
        missing = self.base / "missing"

        found, diagnostics = discover([missing], [incomplete, missing])

        self.assertEqual(found, [])
        self.assertEqual(len(diagnostics), 3)
        self.assertTrue(any(str(missing) in message for message in diagnostics))
        self.assertTrue(any(str(incomplete) in message and "initialized" in message for message in diagnostics))

    def test_existing_empty_beads_directory_is_a_candidate(self):
        project = self.base / "empty"
        (project / ".beads").mkdir(parents=True)

        found, diagnostics = discover([], [project])

        self.assertEqual(found, [project.resolve()])
        self.assertEqual(diagnostics, [])

    def test_explicit_symlink_loop_is_a_diagnostic_instead_of_aborting_scan(self):
        loop = self.base / "loop"
        loop.symlink_to(loop)
        found, diagnostics = discover([loop], [loop])
        self.assertEqual(found, [])
        self.assertEqual(len(diagnostics), 2)
        self.assertTrue(all(str(loop) in message for message in diagnostics))

    def test_walk_permission_errors_are_reported(self):
        root = self.base / "root"
        root.mkdir()

        def failing_walk(path, **kwargs):
            kwargs["onerror"](PermissionError(13, "Permission denied", str(root / "locked")))
            return iter(())

        with patch("beads_dashboard.discovery.os.walk", failing_walk):
            found, diagnostics = discover([root], [])

        self.assertEqual(found, [])
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("Permission denied", diagnostics[0])


if __name__ == "__main__":
    unittest.main()
