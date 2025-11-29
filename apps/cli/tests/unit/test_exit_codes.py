"""Tests for CLI exit code consistency

Exit Code Standards:
- 0: Success
- 1: General errors (authentication, API errors, validation errors)
- 2: Invalid arguments/usage (reserved for Typer)
"""

import pytest
from typer.testing import CliRunner

from cli.commands import main_cli

runner = CliRunner()


@pytest.mark.unit
class TestExitCodeConsistency:
    """Tests for consistent exit codes across commands"""

    def test_invalid_command_returns_nonzero(self):
        """Test that invalid commands return non-zero exit code"""
        result = runner.invoke(main_cli, ["invalid-command"])
        assert result.exit_code != 0

    def test_login_empty_key_returns_one(self, mock_home_dir, mocker):
        """Test that login with empty key returns exit code 1"""
        # Mock Prompt to prevent interactive blocking
        mock_prompt = mocker.patch("cli.commands.login.Prompt")
        mock_prompt.ask.return_value = ""

        result = runner.invoke(main_cli, ["login"])
        assert result.exit_code == 1

    def test_login_invalid_key_returns_one(self, mock_home_dir, mocker):
        """Test that login with invalid key returns exit code 1"""
        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.workspaces.list_workspaces.side_effect = Exception("Invalid key")

        result = runner.invoke(main_cli, ["login", "bad_key"])
        assert result.exit_code == 1

    def test_unauthenticated_command_returns_one(self, mock_home_dir):
        """Test that commands requiring auth return exit code 1 when not authenticated"""
        # With no config file and no env vars, deploy should fail
        result = runner.invoke(main_cli, ["deploy"])

        # Should fail with exit code 1 due to authentication check
        assert result.exit_code == 1
