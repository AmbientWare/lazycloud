"""Tests for CLI exit code consistency

Exit Code Standards:
- 0: Success
- 1: General errors (authentication, API errors, validation errors)
- 2: Invalid arguments/usage (reserved for Typer)
"""

import httpx
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

    def test_login_connection_error_returns_one(self, mock_config_dir, mocker):
        """Test that login with connection error returns exit code 1"""
        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.auth.get_config.side_effect = httpx.RequestError("Connection refused")

        result = runner.invoke(main_cli, ["login"])
        assert result.exit_code == 1

    def test_login_auth_failure_returns_one(self, mock_config_dir, mocker):
        """Test that login with auth failure returns exit code 1"""
        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.auth.get_config.return_value = {"workos_client_id": "test_client"}
        mock_api.auth.request_device_authorization.return_value = {
            "device_code": "device_123",
            "user_code": "ABC-123",
            "verification_uri": "https://auth.example.com/verify",
            "verification_uri_complete": "https://auth.example.com/verify?code=ABC-123",
        }
        mock_api.auth.poll_for_tokens.side_effect = Exception("Token polling timeout")

        mocker.patch("cli.commands.login.webbrowser")

        result = runner.invoke(main_cli, ["login"])
        assert result.exit_code == 1

    def test_unauthenticated_command_returns_one(self, mock_config_dir):
        """Test that commands requiring auth return exit code 1 when not authenticated"""
        # With no config file and no env vars, deploy should fail
        result = runner.invoke(main_cli, ["deploy"])

        # Should fail with exit code 1 due to authentication check
        assert result.exit_code == 1
