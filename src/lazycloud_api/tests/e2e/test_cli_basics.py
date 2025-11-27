"""E2E tests for basic CLI commands.

Run with: pytest src/lazycloud_api/tests/e2e/test_cli_basics.py -v -s
"""

import pytest

from lazycloud_api.tests.e2e.conftest import get_output, requires_api, run_cli

pytestmark = [
    pytest.mark.e2e,
    requires_api,
]


class TestCLIBasics:
    """Basic CLI sanity tests."""

    def test_cli_help(self):
        """Verify CLI is accessible."""
        result = run_cli("--help")
        assert result.returncode == 0
        assert "lazycloud" in result.stdout.lower() or "deploy" in result.stdout.lower()

    def test_cli_version(self):
        """Verify CLI version command works."""
        result = run_cli("--version")
        # May return 0 or have version info in output
        assert result.returncode == 0 or "version" in get_output(result)

    def test_cli_init_help(self):
        """Verify init command help is accessible."""
        result = run_cli("init", "--help")
        assert result.returncode == 0
        assert "name" in result.stdout.lower()

    def test_cli_deploy_help(self):
        """Verify deploy command help is accessible."""
        result = run_cli("deploy", "--help")
        assert result.returncode == 0
        assert "service" in result.stdout.lower() or "yes" in result.stdout.lower()

    def test_cli_workspace_help(self):
        """Verify workspace command help is accessible."""
        result = run_cli("workspace", "--help")
        assert result.returncode == 0
        assert "list" in result.stdout.lower() or "create" in result.stdout.lower()
