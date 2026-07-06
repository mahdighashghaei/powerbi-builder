"""Phase 6.2 tests — deploy engine + ADK fabric tools."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fabric import fab_cli
from fabric.deploy import (
    DeployResult,
    PbipLayout,
    deploy,
    resolve_layout,
)
from adk.tools.fabric_tools import (
    check_fab_auth,
    check_fab_installation,
    deploy_pbip_to_fabric,
    list_fabric_items,
    list_fabric_workspaces,
    preview_pbip_for_deploy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pbip(root: Path, name: str = "Demo") -> Path:
    """Create a minimal PBIP folder structure on disk."""
    sm = root / f"{name}.SemanticModel"
    rp = root / f"{name}.Report"
    (sm / "definition").mkdir(parents=True, exist_ok=True)
    (rp / "definition").mkdir(parents=True, exist_ok=True)
    (sm / "definition.pbism").write_text("{}", encoding="utf-8")
    (rp / "definition.pbir").write_text("{}", encoding="utf-8")
    return root


def _mock_proc(stdout: str = "", returncode: int = 0, stderr: str = ""):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


# ---------------------------------------------------------------------------
# resolve_layout
# ---------------------------------------------------------------------------

class TestResolveLayout(unittest.TestCase):

    def test_finds_sm_and_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_pbip(Path(td), "Demo")
            layout = resolve_layout(root)
            self.assertIsInstance(layout, PbipLayout)
            self.assertEqual(layout.semantic_model_name, "Demo")
            self.assertEqual(layout.report_name, "Demo")
            self.assertTrue(layout.semantic_model_dir.is_dir())

    def test_missing_path(self):
        with self.assertRaises(FileNotFoundError):
            resolve_layout("/this/does/not/exist")

    def test_no_semantic_model(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "X.Report").mkdir()
            with self.assertRaises(FileNotFoundError):
                resolve_layout(td)

    def test_no_report(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "X.SemanticModel").mkdir()
            with self.assertRaises(FileNotFoundError):
                resolve_layout(td)

    def test_multiple_projects_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pbip(Path(td), "A")
            _make_pbip(Path(td), "B")
            with self.assertRaises(ValueError):
                resolve_layout(td)


# ---------------------------------------------------------------------------
# deploy() — mocked fab_cli
# ---------------------------------------------------------------------------

class TestDeploy(unittest.TestCase):

    def test_dry_run_invokes_no_subprocess(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pbip(Path(td), "Demo")
            with patch("fabric.fab_cli.subprocess.run") as mock_run:
                result = deploy(td, "Dev", dry_run=True)
        mock_run.assert_not_called()
        self.assertTrue(result.ok)
        self.assertTrue(result.dry_run)
        # 2 actions: model + report
        self.assertEqual(len(result.actions), 2)
        self.assertEqual(result.actions[0]["kind"], "SemanticModel")
        self.assertEqual(result.actions[1]["kind"], "Report")

    def test_invalid_mode(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pbip(Path(td), "Demo")
            result = deploy(td, "Dev", mode="bogus", dry_run=True)
        self.assertFalse(result.ok)
        self.assertIn("invalid mode", result.error)

    def test_missing_pbip_returns_error(self):
        result = deploy("/no/such/path", "Dev", dry_run=True)
        self.assertFalse(result.ok)
        self.assertIn("not found", result.error)

    def test_skip_model_skips_first_action(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pbip(Path(td), "Demo")
            result = deploy(td, "Dev", dry_run=True, skip_model=True)
        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0]["kind"], "Report")

    def test_skip_report_skips_second_action(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pbip(Path(td), "Demo")
            result = deploy(td, "Dev", dry_run=True, skip_report=True)
        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0]["kind"], "SemanticModel")

    def test_create_when_item_missing(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pbip(Path(td), "Demo")
            with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
                 patch("fabric.fab_cli.subprocess.run",
                       side_effect=[
                           _mock_proc(stdout=""),     # list_items -> empty
                           _mock_proc(),              # import model
                           _mock_proc(stdout=""),     # list_items -> empty
                           _mock_proc(),              # import report
                       ]):
                result = deploy(td, "Dev", mode="auto")
        self.assertTrue(result.ok)
        self.assertEqual(result.actions[0]["action"], "create")
        self.assertEqual(result.actions[1]["action"], "create")

    def test_update_when_item_exists(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pbip(Path(td), "Demo")
            with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
                 patch("fabric.fab_cli.subprocess.run",
                       side_effect=[
                           _mock_proc(stdout="Demo.SemanticModel\n"),
                           _mock_proc(),  # import
                           _mock_proc(stdout="Demo.Report\n"),
                           _mock_proc(),  # import
                       ]):
                result = deploy(td, "Dev", mode="auto")
        self.assertTrue(result.ok)
        self.assertEqual(result.actions[0]["action"], "update")
        self.assertEqual(result.actions[1]["action"], "update")

    def test_fab_not_installed_fails_fast(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pbip(Path(td), "Demo")
            with patch("fabric.fab_cli.shutil.which", return_value=None):
                result = deploy(td, "Dev", dry_run=False)
        self.assertFalse(result.ok)
        self.assertIn("not installed", result.error)

    def test_model_failure_aborts_before_report(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pbip(Path(td), "Demo")
            with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
                 patch("fabric.fab_cli.subprocess.run",
                       side_effect=[
                           _mock_proc(stdout=""),     # list_items (model)
                           _mock_proc(returncode=1, stderr="auth failed"),  # import model
                       ]):
                result = deploy(td, "Dev", mode="auto")
        self.assertFalse(result.ok)
        # exactly one action recorded — report skipped
        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0]["kind"], "SemanticModel")


# ---------------------------------------------------------------------------
# ADK tool wrappers
# ---------------------------------------------------------------------------

class TestAdkWrappers(unittest.TestCase):

    def test_check_fab_installation_missing(self):
        with patch("fabric.fab_cli.shutil.which", return_value=None):
            out = check_fab_installation()
        self.assertFalse(out["installed"])
        self.assertIn("pip install", out["error"])

    def test_check_fab_installation_present(self):
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc(stdout="fab 2.0\n")):
            out = check_fab_installation()
        self.assertTrue(out["installed"])
        self.assertIn("fab", out["version"])

    def test_check_fab_auth_signed_in(self):
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc(stdout="Logged in")):
            out = check_fab_auth()
        self.assertTrue(out["ok"])
        self.assertTrue(out["signed_in"])

    def test_list_workspaces_wrapper(self):
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc(stdout="Dev.Workspace\nProd.Workspace\n")):
            out = list_fabric_workspaces()
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 2)

    def test_list_items_wrapper(self):
        with patch("fabric.fab_cli.shutil.which", return_value="/usr/bin/fab"), \
             patch("fabric.fab_cli.subprocess.run",
                   return_value=_mock_proc(stdout="Sales.SemanticModel\nDash.Report\n")):
            out = list_fabric_items("Dev")
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 2)

    def test_preview_pbip_for_deploy(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pbip(Path(td), "Demo")
            out = preview_pbip_for_deploy(td)
        self.assertTrue(out["ok"])
        self.assertEqual(out["semantic_model_name"], "Demo")
        self.assertEqual(out["report_name"], "Demo")

    def test_preview_pbip_missing(self):
        out = preview_pbip_for_deploy("/nonexistent/xyz")
        self.assertFalse(out["ok"])

    def test_deploy_pbip_to_fabric_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            _make_pbip(Path(td), "Demo")
            with patch("fabric.fab_cli.subprocess.run") as mock_run:
                out = deploy_pbip_to_fabric(td, "Dev", dry_run=True)
        mock_run.assert_not_called()
        self.assertTrue(out["ok"])
        self.assertTrue(out["dry_run"])
        self.assertEqual(len(out["actions"]), 2)

    def test_deploy_pbip_to_fabric_real_invokes_subprocess(self):
        """dry_run=False must actually invoke the fab CLI (subprocess.run),
        proving the real upload path is wired through end-to-end."""
        with tempfile.TemporaryDirectory() as td:
            _make_pbip(Path(td), "Demo")
            mock_proc = MagicMock(returncode=0, stdout="ok", stderr="")
            with patch("fabric.fab_cli.subprocess.run", return_value=mock_proc) as mock_run, \
                 patch("fabric.fab_cli.is_installed", return_value=True), \
                 patch("fabric.fab_cli.item_exists", return_value=False):
                out = deploy_pbip_to_fabric(td, "Dev", dry_run=False)
        # subprocess.run was called (real deploy path), at least twice (model + report)
        self.assertGreaterEqual(mock_run.call_count, 2)
        self.assertFalse(out["dry_run"])


if __name__ == "__main__":
    unittest.main()
