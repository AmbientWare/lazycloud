"""Unit tests for login command"""

import pytest
from typer.testing import CliRunner

import cli.config
from cli.commands.login import app

runner = CliRunner()


@pytest.mark.unit
class TestLoginCommand:
    """Tests for login command with OAuth device flow"""

    def test_login_success(self, mock_config_dir, sample_workspaces, mocker):
        """Test successful login flow"""
        # Mock API calls
        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.auth.get_config.return_value = {"workos_client_id": "test_client"}
        mock_api.auth.request_device_authorization.return_value = {
            "device_code": "device_123",
            "user_code": "ABC-123",
            "verification_uri": "https://auth.example.com/verify",
            "verification_uri_complete": "https://auth.example.com/verify?code=ABC-123",
            "expires_in": 300,
            "interval": 5,
        }
        mock_api.auth.poll_for_tokens.return_value = {
            "access_token": "access_token_123",
            "refresh_token": "refresh_token_456",
            "user": {"first_name": "Test", "email": "test@example.com"},
        }
        mock_api.workspaces.list_workspaces.return_value = sample_workspaces

        # Mock webbrowser to prevent actual browser opening
        mocker.patch("cli.commands.login.webbrowser")

        result = runner.invoke(app, [])

        assert result.exit_code == 0

        # Verify config was saved
        assert cli.config.CONFIG_FILE.exists()
        content = cli.config.CONFIG_FILE.read_text()
        assert 'access_token = "access_token_123"' in content
        assert 'refresh_token = "refresh_token_456"' in content
        assert 'id = "ws_123456"' in content

    def test_login_connection_error(self, mock_config_dir, mocker):
        """Test login with connection error"""
        import httpx

        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.auth.get_config.side_effect = httpx.RequestError("Connection refused")

        result = runner.invoke(app, [])

        assert result.exit_code == 1

    def test_login_auth_server_error(self, mock_config_dir, mocker):
        """Test login with auth server error"""
        import httpx

        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.auth.get_config.return_value = {"workos_client_id": "test_client"}

        response = httpx.Response(500, text="Internal Server Error")
        mock_api.auth.request_device_authorization.side_effect = (
            httpx.HTTPStatusError("Error", request=None, response=response)
        )

        result = runner.invoke(app, [])

        assert result.exit_code == 1

    def test_login_no_personal_workspace(self, mock_config_dir, mocker):
        """Test login when no personal workspace is found"""
        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.auth.get_config.return_value = {"workos_client_id": "test_client"}
        mock_api.auth.request_device_authorization.return_value = {
            "device_code": "device_123",
            "user_code": "ABC-123",
            "verification_uri": "https://auth.example.com/verify",
            "verification_uri_complete": "https://auth.example.com/verify?code=ABC-123",
        }
        mock_api.auth.poll_for_tokens.return_value = {
            "access_token": "access_token_123",
        }
        # Return only non-personal workspaces
        mock_api.workspaces.list_workspaces.return_value = [
            {"id": "ws_team", "name": "Team Workspace", "is_personal": False}
        ]

        mocker.patch("cli.commands.login.webbrowser")

        result = runner.invoke(app, [])

        assert result.exit_code == 1

    def test_login_token_polling_failure(self, mock_config_dir, mocker):
        """Test login when token polling fails"""
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

        result = runner.invoke(app, [])

        assert result.exit_code == 1

    def test_login_clears_tokens_on_workspace_error(self, mock_config_dir, mocker):
        """Test that tokens are cleared if workspace configuration fails"""
        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.auth.get_config.return_value = {"workos_client_id": "test_client"}
        mock_api.auth.request_device_authorization.return_value = {
            "device_code": "device_123",
            "user_code": "ABC-123",
            "verification_uri": "https://auth.example.com/verify",
            "verification_uri_complete": "https://auth.example.com/verify?code=ABC-123",
        }
        mock_api.auth.poll_for_tokens.return_value = {
            "access_token": "access_token_123",
        }
        mock_api.workspaces.list_workspaces.side_effect = Exception("API Error")

        mocker.patch("cli.commands.login.webbrowser")

        result = runner.invoke(app, [])

        assert result.exit_code == 1

        # Verify tokens were cleared
        from cli.config import CLIConfig

        config = CLIConfig()
        assert config.access_token is None
