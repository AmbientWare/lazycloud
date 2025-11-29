"""Unit tests for login command"""

import pytest
from cli.commands.login import app
from typer.testing import CliRunner

runner = CliRunner()


@pytest.mark.unit
class TestLoginCommand:
    """Tests for login command"""

    def test_login_with_valid_api_key(self, mock_home_dir, sample_workspaces, mocker):
        """Test login with valid API key as argument"""
        # Mock the API client
        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.workspaces.list_workspaces.return_value = sample_workspaces

        result = runner.invoke(app, ["test_key_123"])

        assert result.exit_code == 0
        # Verify API was called
        mock_api.workspaces.list_workspaces.assert_called_once()

        # Verify config was saved (actual behavior, not UI text)
        config_file = mock_home_dir / ".lazycloud"
        assert config_file.exists()
        assert "API_KEY=test_key_123" in config_file.read_text()

    def test_login_with_empty_api_key(self, mock_home_dir, mocker):
        """Test login with empty API key as argument"""
        # Mock console to avoid rich output issues
        mocker.patch("cli.commands.login.console")

        # Mock Prompt to prevent interactive blocking
        mock_prompt = mocker.patch("cli.commands.login.Prompt")
        mock_prompt.ask.return_value = ""

        result = runner.invoke(app, [])

        assert result.exit_code == 1

    def test_login_with_whitespace_api_key(self, mock_home_dir):
        """Test login with whitespace-only API key"""
        result = runner.invoke(app, ["   "])

        assert result.exit_code == 1

    def test_login_with_invalid_api_key(self, mock_home_dir, mocker):
        """Test login with invalid API key that fails validation"""
        # Mock the API client to raise an exception
        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.workspaces.list_workspaces.side_effect = Exception(
            "Unauthorized: Invalid API key"
        )

        result = runner.invoke(app, ["invalid_key"])

        assert result.exit_code == 1

        # Verify the API key was not saved (cleared after failure)
        from cli.config import config

        # Need to reload config to see cleared state
        config._load_config()
        assert config.api_key is None

    def test_login_no_personal_workspace(self, mock_home_dir, mocker):
        """Test login when no personal workspace is found"""
        # Mock the API to return workspaces without a personal one
        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.workspaces.list_workspaces.return_value = [
            {
                "id": "ws_123",
                "name": "Team Workspace",
                "is_personal": False,
                "role": "member",
            }
        ]

        result = runner.invoke(app, ["valid_key"])

        assert result.exit_code == 1

        # Verify API key was cleared (important behavior)
        from cli.config import config

        config._load_config()
        assert config.api_key is None

    def test_login_network_error(self, mock_home_dir, mocker):
        """Test login with network error"""
        import httpx

        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.workspaces.list_workspaces.side_effect = httpx.ConnectError(
            "Connection refused"
        )

        result = runner.invoke(app, ["test_key"])

        assert result.exit_code == 1

        # Verify API key was cleared after network error
        from cli.config import config

        config._load_config()
        assert config.api_key is None

    def test_login_saves_workspace_info(self, mock_home_dir, sample_workspaces, mocker):
        """Test that login saves workspace information to config"""
        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.workspaces.list_workspaces.return_value = sample_workspaces

        result = runner.invoke(app, ["test_key_123"])

        assert result.exit_code == 0

        # Verify config file was created with workspace info
        config_file = mock_home_dir / ".lazycloud"
        assert config_file.exists()

        content = config_file.read_text()
        assert "API_KEY=test_key_123" in content
        assert "ACTIVE_WORKSPACE_ID=ws_123456" in content
        assert "ACTIVE_WORKSPACE_NAME=My Workspace" in content

    def test_login_interactive_mode(self, mock_home_dir, sample_workspaces, mocker):
        """Test login with interactive prompt (no argument)"""
        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.workspaces.list_workspaces.return_value = sample_workspaces

        # Mock Prompt.ask to return a key without blocking
        mock_prompt = mocker.patch("cli.commands.login.Prompt")
        mock_prompt.ask.return_value = "interactive_key"

        result = runner.invoke(app, [])

        assert result.exit_code == 0

        # Verify the key from interactive prompt was saved
        config_file = mock_home_dir / ".lazycloud"
        assert "API_KEY=interactive_key" in config_file.read_text()

    def test_login_clears_api_key_on_failure(self, mock_home_dir, mocker):
        """Test that failed login clears the API key"""
        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.workspaces.list_workspaces.side_effect = Exception("API Error")

        result = runner.invoke(app, ["failed_key"])

        assert result.exit_code == 1

        # Verify API key was cleared
        from cli.config import config

        config._load_config()
        assert config.api_key is None

    def test_login_strips_whitespace_from_key(
        self, mock_home_dir, sample_workspaces, mocker
    ):
        """Test that login strips whitespace from API key"""
        mock_api = mocker.patch("cli.commands.login.api")
        mock_api.workspaces.list_workspaces.return_value = sample_workspaces

        result = runner.invoke(app, ["  test_key_with_spaces  "])

        assert result.exit_code == 0

        # Verify stripped key was saved
        config_file = mock_home_dir / ".lazycloud"
        content = config_file.read_text()
        assert "API_KEY=test_key_with_spaces" in content
        assert "API_KEY=  test_key_with_spaces  " not in content
