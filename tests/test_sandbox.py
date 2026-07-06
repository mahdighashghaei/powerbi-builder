"""Tests for sandbox isolation (Wave C2).

Verifies:
  * LocalSandbox restricts the environment to an allowlist (no secret leak).
  * LocalSandbox pins the binary path (absolute path is used verbatim).
  * A missing pinned binary is refused without executing.
  * ContainerSandbox is a safe documented stub (refuses, no crash).
  * get_sandbox() returns LocalSandbox by default.
  * fab_cli._run still works under dry_run (no subprocess) after hardening.
  * restricted_env honours the FAB_* prefix allow rule.

Stdlib unittest — runs under ``python -m pytest tests/ -v``.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class TestRestrictedEnv(unittest.TestCase):
    """The subprocess env only carries allowlisted variables."""

    def setUp(self):
        # Set a fake secret that must NOT leak into the subprocess env.
        os.environ["POWERBI_FAKE_SECRET"] = "leak-me-not"

    def tearDown(self):
        os.environ.pop("POWERBI_FAKE_SECRET", None)

    def test_secret_not_in_restricted_env(self):
        from security.sandbox import restricted_env  # noqa: E402

        env = restricted_env()
        self.assertNotIn("POWERBI_FAKE_SECRET", env)

    def test_path_is_allowed_through(self):
        from security.sandbox import restricted_env  # noqa: E402

        env = restricted_env()
        # PATH should be present (it's in the default allowlist).
        if "PATH" in os.environ:
            self.assertIn("PATH", env)

    def test_fab_prefix_allowed(self):
        os.environ["FAB_AUTH_TOKEN"] = "tok-123"
        try:
            from security.sandbox import restricted_env  # noqa: E402

            env = restricted_env()
            self.assertIn("FAB_AUTH_TOKEN", env)
        finally:
            os.environ.pop("FAB_AUTH_TOKEN", None)


class TestLocalSandbox(unittest.TestCase):
    """LocalSandbox: binary pinning + execution."""

    def test_pinned_missing_binary_refused(self):
        from security.sandbox import LocalSandbox  # noqa: E402

        sb = LocalSandbox(binary="/nonexistent/xyz/fab")
        result = sb.run(["--version"], timeout=5, cwd=None)
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["stderr"])

    def test_real_command_runs(self):
        # Use the Python interpreter itself as a known-good binary.
        from security.sandbox import LocalSandbox  # noqa: E402

        sb = LocalSandbox(binary=sys.executable)
        # sandbox.run expects the full command (binary + args), mirroring how
        # fab_cli._run builds ``cmd = [binary, *args]`` before calling run.
        result = sb.run([sys.executable, "-c", "print('hello')"], timeout=10, cwd=None)
        self.assertTrue(result["ok"], f"failed: {result}")
        self.assertIn("hello", result["stdout"])

    def test_restricted_env_applied(self):
        from security.sandbox import LocalSandbox  # noqa: E402

        os.environ["POWERBI_FAKE_SECRET"] = "leak-me-not"
        try:
            sb = LocalSandbox(binary=sys.executable)
            # The child prints its own env; the secret must not appear.
            result = sb.run(
                [sys.executable, "-c", "import os; print('SECRET=' + os.environ.get('POWERBI_FAKE_SECRET','ABSENT'))"],
                timeout=10,
                cwd=None,
            )
            self.assertIn("ABSENT", result["stdout"])
        finally:
            os.environ.pop("POWERBI_FAKE_SECRET", None)


class TestContainerSandbox(unittest.TestCase):
    """ContainerSandbox is a documented stub that refuses safely."""

    def test_refuses_without_runtime(self):
        from security.sandbox import ContainerSandbox  # noqa: E402

        sb = ContainerSandbox()
        result = sb.run(["--version"], timeout=5, cwd=None)
        self.assertFalse(result["ok"])
        self.assertIn("not configured", result["stderr"])


class TestGetSandbox(unittest.TestCase):
    """get_sandbox returns the right runner per config."""

    def tearDown(self):
        os.environ.pop("POWERBI_SANDBOX_MODE", None)

    def test_default_is_local(self):
        from security.sandbox import LocalSandbox, get_sandbox  # noqa: E402

        os.environ.pop("POWERBI_SANDBOX_MODE", None)
        self.assertIsInstance(get_sandbox(), LocalSandbox)

    def test_container_mode(self):
        from security.sandbox import ContainerSandbox, get_sandbox  # noqa: E402

        os.environ["POWERBI_SANDBOX_MODE"] = "container"
        self.assertIsInstance(get_sandbox(), ContainerSandbox)


class TestFabCliHardening(unittest.TestCase):
    """fab_cli._run still works under dry_run after the sandbox rewire."""

    def test_dry_run_does_not_invoke_subprocess(self):
        from fabric.fab_cli import _run  # noqa: E402

        result = _run(["--version"], dry_run=True)
        self.assertTrue(result.ok)
        self.assertTrue(result.dry_run)

    def test_pinned_binary_path_in_command(self):
        os.environ["POWERBI_FAB_BINARY"] = "/custom/path/fab"
        try:
            from fabric.fab_cli import _run  # noqa: E402

            result = _run(["--version"], dry_run=True)
            self.assertEqual(result.command[0], "/custom/path/fab")
        finally:
            os.environ.pop("POWERBI_FAB_BINARY", None)


if __name__ == "__main__":
    unittest.main()
