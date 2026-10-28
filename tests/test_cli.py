import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from beads_dashboard import cli


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_config_reads_lists_and_resolves_relative_to_config(self):
        config = self.base / "config.toml"
        config.write_text('roots = ["src", "~/work"]\nprojects = ["app"]\n')
        with patch.dict(os.environ, {"HOME": str(self.base)}):
            roots, projects = cli.load_config(config)
        self.assertEqual(roots, [(self.base / "src").resolve(), (self.base / "work").resolve()])
        self.assertEqual(projects, [(self.base / "app").resolve()])

    def test_config_rejects_unknown_keys_and_wrong_types(self):
        config = self.base / "config.toml"
        for contents in ('root = ["x"]', 'roots = "x"', 'projects = [1]'):
            config.write_text(contents)
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                cli.load_config(config)

    def test_cli_targets_replace_config_targets(self):
        config = self.base / "config.toml"
        config.write_text('roots = ["configured"]\nprojects = ["configured-project"]\n')
        cli_root = self.base / "cli-root"
        cli_project = self.base / "cli-project"
        args = cli.parser().parse_args(["--config", str(config), "--root", str(cli_root), "--project", str(cli_project)])
        roots, projects = cli.resolve_targets(args)
        self.assertEqual(roots, [cli_root.resolve()])
        self.assertEqual(projects, [cli_project.resolve()])

    def test_missing_explicit_config_is_value_error(self):
        with self.assertRaises(ValueError):
            cli.load_config(self.base / "missing.toml")

    def test_once_prints_snapshot_and_returns_failure_for_project_error(self):
        project = SimpleNamespace(key="k", path=self.base, name="demo", branch="main")
        state = SimpleNamespace(project=project, issues={"i": {"id": "i"}}, error="read failed",
                                last_success=datetime(2026, 1, 2, tzinfo=timezone.utc), duration=0.2)
        monitor = SimpleNamespace(states={"k": state}, diagnostics=[])

        async def once():
            return None

        monitor.once = once
        out = SimpleNamespace(write=lambda value: output.append(value))
        output = []
        with patch("beads_dashboard.monitor.Monitor", return_value=monitor):
            result = cli.main(["--once", "--root", str(self.base)], stdout=out)
        self.assertEqual(result, 1)
        payload = json.loads("".join(output))
        self.assertEqual(payload["projects"][0]["issues"], [{"id": "i"}])
        self.assertEqual(payload["projects"][0]["last_success"], "2026-01-02T00:00:00+00:00")
        self.assertEqual(payload["projects"][0]["error"], "read failed")

    def test_once_does_not_import_ui(self):
        project = SimpleNamespace(key="k", path=self.base, name="demo", branch="main")
        state = SimpleNamespace(project=project, issues={}, error=None, last_success=None, duration=0)
        monitor = SimpleNamespace(states={"k": state}, diagnostics=[])

        async def once():
            return None

        monitor.once = once
        real_import = __import__

        def no_ui_import(name, *args, **kwargs):
            if name == "beads_dashboard.app":
                self.fail("one-shot mode imported the UI")
            return real_import(name, *args, **kwargs)

        with patch("beads_dashboard.monitor.Monitor", return_value=monitor), patch("builtins.__import__", no_ui_import):
            self.assertEqual(cli.main(["--once"], stdout=SimpleNamespace(write=lambda _: None)), 0)

    def test_once_interruption_does_not_call_async_stop(self):
        monitor = SimpleNamespace(states={}, diagnostics=[], stop=AsyncMock())

        async def once():
            raise KeyboardInterrupt

        monitor.once = once
        with patch("beads_dashboard.monitor.Monitor", return_value=monitor):
            self.assertEqual(cli.main(["--once"], stdout=SimpleNamespace(write=lambda _: None)), 130)
        monitor.stop.assert_not_awaited()

    def test_interactive_interruption_leaves_teardown_to_textual(self):
        monitor = SimpleNamespace(stop=AsyncMock())
        with patch("beads_dashboard.monitor.Monitor", return_value=monitor), \
             patch("beads_dashboard.app.DashboardApp") as app:
            app.return_value.run.side_effect = KeyboardInterrupt
            self.assertEqual(cli.main([]), 130)
        monitor.stop.assert_not_awaited()

    def test_help_explains_target_sources(self):
        help_text = cli.parser().format_help()
        self.assertIn("scan a directory recursively", help_text)
        self.assertIn("watch an explicit project directory", help_text)
        self.assertIn("$XDG_CONFIG_HOME", help_text)


if __name__ == "__main__":
    unittest.main()
