"""Phase 6.1 tests — fab CLI wrapper.

All subprocess calls are mocked so the tests run without fab installed.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fabric import fab_cli


def _mock_proc(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Return a fake CompletedProcess."""
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


class TestInstalledProbe(unittest.TestCase):

    def test_is_installed_yes(self):
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"):
            self.assertTrue(fab_cli.is_installed())

    def test_is_installed_no(self):
        with patch("fabric.fab_cli.shutil.which", return_value=None):
            self.assertFalse(fab_cli.is_installed())

    def test_run_returns_error_when_not_installed(self):
        with patch("fabric.fab_cli.shutil.which", return_value=None):
            res = fab_cli._run(["--version"])
        self.assertFalse(res.ok)
        self.assertIn("not found on PATH", res.error)


class TestDryRun(unittest.TestCase):

    def test_dry_run_does_not_invoke_subprocess(self):
        with patch("fabric.fab_cli.subprocess.run") as mock_run:
            res = fab_cli._run(["--version"], dry_run=True)
        mock_run.assert_not_called()
        self.assertTrue(res.ok)
        self.assertTrue(res.dry_run)
        self.assertEqual(res.command[0], "fab")


class TestVersion(unittest.TestCase):

    def test_version_ok(self):
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc(stdout="fab 1.2.3\n")):
            res = fab_cli.version()
        self.assertTrue(res.ok)
        self.assertEqual(res.data, "fab 1.2.3")

    def test_version_fail(self):
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc(returncode=1, stderr="boom\n")):
            res = fab_cli.version()
        self.assertFalse(res.ok)
        self.assertIn("boom", res.error)


class TestAuthStatus(unittest.TestCase):

    def test_signed_in(self):
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc(stdout="Logged in as foo@bar")):
            res = fab_cli.auth_status()
        self.assertTrue(res.ok)
        self.assertTrue(res.data["signed_in"])

    def test_not_signed_in(self):
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc(returncode=1, stderr="Not authenticated")):
            res = fab_cli.auth_status()
        self.assertFalse(res.ok)
        self.assertFalse(res.data["signed_in"])


class TestListWorkspaces(unittest.TestCase):

    def test_parses_workspace_lines(self):
        out = "Dev.Workspace\nProd.Workspace\nIgnoreThis\n"
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc(stdout=out)):
            res = fab_cli.list_workspaces()
        self.assertTrue(res.ok)
        names = [w["name"] for w in res.data]
        self.assertEqual(names, ["Dev", "Prod"])
        self.assertEqual(res.data[0]["type"], "Workspace")

    def test_empty_workspaces(self):
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc(stdout="")):
            res = fab_cli.list_workspaces()
        self.assertTrue(res.ok)
        self.assertEqual(res.data, [])


class TestListItems(unittest.TestCase):

    def test_parses_item_lines(self):
        out = "Sales.SemanticModel\nDashboard.Report\nLake.Lakehouse\n"
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc(stdout=out)):
            res = fab_cli.list_items("Dev")
        self.assertTrue(res.ok)
        self.assertEqual(len(res.data), 3)
        types = {i["type"] for i in res.data}
        self.assertEqual(types, {"SemanticModel", "Report", "Lakehouse"})


class TestGetId(unittest.TestCase):

    def test_strips_quotes(self):
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc(stdout='"abc-1234"\n')):
            res = fab_cli.get_id("Dev.Workspace")
        self.assertEqual(res.data, "abc-1234")


class TestImportItem(unittest.TestCase):

    def test_import_builds_correct_command(self):
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc()) as mock_run:
            res = fab_cli.import_item(
                "Dev", "MyModel", "SemanticModel",
                "/tmp/my.SemanticModel",
            )
        self.assertTrue(res.ok)
        cmd = mock_run.call_args.args[0]
        self.assertIn("import", cmd)
        self.assertIn("Dev.Workspace/MyModel.SemanticModel", cmd)
        self.assertIn("-i", cmd)
        self.assertIn("/tmp/my.SemanticModel", cmd)
        self.assertIn("-f", cmd)
        self.assertEqual(res.data["type"], "SemanticModel")

    def test_import_without_force(self):
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc()) as mock_run:
            fab_cli.import_item(
                "Dev", "MyModel", "SemanticModel",
                "/tmp/x.SemanticModel", force=False,
            )
        cmd = mock_run.call_args.args[0]
        self.assertNotIn("-f", cmd)

    def test_import_dry_run(self):
        with patch("fabric.fab_cli.subprocess.run") as mock_run:
            res = fab_cli.import_item(
                "Dev", "X", "Report", "/tmp/X.Report",
                dry_run=True,
            )
        mock_run.assert_not_called()
        self.assertTrue(res.dry_run)


class TestExportItem(unittest.TestCase):

    def test_export_builds_correct_command(self):
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc()) as mock_run:
            res = fab_cli.export_item(
                "Dev", "MyModel", "SemanticModel", "/tmp/out",
            )
        cmd = mock_run.call_args.args[0]
        self.assertIn("export", cmd)
        self.assertIn("-o", cmd)
        self.assertIn("/tmp/out", cmd)
        self.assertTrue(res.ok)


class TestItemExists(unittest.TestCase):

    def test_finds_existing_item(self):
        out = "Sales.SemanticModel\nDash.Report\n"
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc(stdout=out)):
            self.assertTrue(fab_cli.item_exists("Dev", "Sales", "SemanticModel"))
            self.assertFalse(fab_cli.item_exists("Dev", "Missing", "Report"))


class TestTimeoutHandling(unittest.TestCase):

    def test_timeout_returns_error(self):
        def boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=["fab"], timeout=5)
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run", side_effect=boom):
            res = fab_cli._run(["ls"], timeout=5)
        self.assertFalse(res.ok)
        self.assertIn("timed out", res.error)


if __name__ == "__main__":
    unittest.main()
