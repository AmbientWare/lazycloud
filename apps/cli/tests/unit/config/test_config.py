"""Unit tests for CLI configuration"""

from pathlib import Path

import pytest
from cli.config import CLIConfig


@pytest.mark.unit
class TestCLIConfig:
    """Tests for CLIConfig class"""

    def test_config_from_env_vars(self, mock_home_dir, monkeypatch):
        """Test configuration from environment variables"""
        monkeypatch.setenv("LAZYCLOUD_API_BASE_URL", "https://api.lazycloud.dev")
        monkeypatch.setenv("LAZYCLOUD_API_VERSION", "v2")

        config = CLIConfig()

        assert config.api_base_url == "https://api.lazycloud.dev"
        assert config.api_version == "v2"
        assert config.api_url == "https://api.lazycloud.dev/v2"

    def test_api_base_url_validation_invalid(self, mock_home_dir):
        """Test API base URL validation rejects invalid URLs"""
        with pytest.raises(ValueError, match="must start with http"):
            CLIConfig(api_base_url="example.com")

    def test_api_base_url_strips_trailing_slash(self, mock_home_dir):
        """Test that trailing slashes are removed from base URL"""
        config = CLIConfig(api_base_url="http://localhost:8000/")
        assert config.api_base_url == "http://localhost:8000"

    def test_load_config_from_file(self, mock_config_file):
        """Test loading configuration from file"""
        mock_config_file(
            api_key="test_key_123",
            workspace_id="ws_123",
            workspace_name="My Workspace",
        )

        config = CLIConfig()

        assert config.api_key == "test_key_123"
        assert config.active_workspace_id == "ws_123"
        assert config.active_workspace_name == "My Workspace"

    def test_set_api_key(self, mock_home_dir):
        """Test setting API key"""
        config = CLIConfig()
        config.set_api_key("new_key_456")

        assert config.api_key == "new_key_456"

        # Verify it's saved to file
        config_file = mock_home_dir / ".lazycloud"
        assert config_file.exists()
        assert "API_KEY=new_key_456" in config_file.read_text()

    def test_clear_api_key(self, mock_config_file):
        """Test clearing API key"""
        mock_config_file(api_key="test_key")

        config = CLIConfig()
        assert config.api_key == "test_key"

        config.clear_api_key()
        assert config.api_key is None

        # Verify it's removed from file
        config_file = Path.home() / ".lazycloud"
        assert "API_KEY" not in config_file.read_text()

    def test_api_key_from_env_var(self, mock_home_dir, monkeypatch):
        """Test API key from environment variable"""
        monkeypatch.setenv("LAZYCLOUD_API_KEY", "env_key_789")

        config = CLIConfig()
        assert config.api_key == "env_key_789"

    def test_api_key_priority_file_over_env(self, mock_config_file, monkeypatch):
        """Test that stored API key takes priority over env var"""
        mock_config_file(api_key="file_key")
        monkeypatch.setenv("LAZYCLOUD_API_KEY", "env_key")

        config = CLIConfig()
        assert config.api_key == "file_key"

    def test_set_active_workspace(self, mock_home_dir):
        """Test setting active workspace"""
        config = CLIConfig()
        config.set_active_workspace("ws_123", "Test Workspace")

        assert config.active_workspace_id == "ws_123"
        assert config.active_workspace_name == "Test Workspace"

        # Verify it's saved to file
        config_file = mock_home_dir / ".lazycloud"
        content = config_file.read_text()
        assert "ACTIVE_WORKSPACE_ID=ws_123" in content
        assert "ACTIVE_WORKSPACE_NAME=Test Workspace" in content

    def test_clear_active_workspace(self, mock_config_file):
        """Test clearing active workspace"""
        mock_config_file(workspace_id="ws_123", workspace_name="Test")

        config = CLIConfig()
        config.clear_active_workspace()

        with pytest.raises(ValueError, match="No active workspace"):
            _ = config.active_workspace_id

        # Verify it's removed from file
        config_file = Path.home() / ".lazycloud"
        content = config_file.read_text()
        assert "ACTIVE_WORKSPACE_ID" not in content
        assert "ACTIVE_WORKSPACE_NAME" not in content

    def test_active_workspace_from_env_var(self, mock_home_dir, monkeypatch):
        """Test active workspace from environment variable"""
        monkeypatch.setenv("LAZYCLOUD_WORKSPACE_ID", "ws_env")

        config = CLIConfig()
        # Should work from env var
        assert config.active_workspace_id == "ws_env"

    def test_active_workspace_priority_file_over_env(
        self, mock_config_file, monkeypatch
    ):
        """Test that stored workspace takes priority over env var"""
        mock_config_file(workspace_id="ws_file")
        monkeypatch.setenv("LAZYCLOUD_WORKSPACE_ID", "ws_env")

        config = CLIConfig()
        assert config.active_workspace_id == "ws_file"

    def test_check_authentication_not_logged_in(self, mock_home_dir):
        """Test authentication check when not logged in"""
        config = CLIConfig()
        is_authed, message = config.check_authentication()

        assert is_authed is False
        assert "Not logged in" in message
        assert "lazycloud login" in message

    def test_check_authentication_no_workspace(self, mock_config_file):
        """Test authentication check when API key set but no workspace"""
        mock_config_file(api_key="test_key")

        config = CLIConfig()
        is_authed, message = config.check_authentication()

        assert is_authed is False
        assert "No workspace configured" in message

    def test_check_authentication_success(self, mock_config_file):
        """Test authentication check when fully configured"""
        mock_config_file(
            api_key="test_key", workspace_id="ws_123", workspace_name="Test"
        )

        config = CLIConfig()
        is_authed, message = config.check_authentication()

        assert is_authed is True
        assert message == ""

    def test_check_authentication_env_var_only(self, mock_home_dir, monkeypatch):
        """Test authentication check with env var only (CI/CD mode)"""
        monkeypatch.setenv("LAZYCLOUD_API_KEY", "env_key")
        monkeypatch.setenv("LAZYCLOUD_WORKSPACE_ID", "ws_env")

        config = CLIConfig()
        is_authed, message = config.check_authentication()

        # Should be valid for CI/CD usage
        assert is_authed is True
        assert message == ""

    def test_active_workspace_name_no_value(self, mock_home_dir):
        """Test accessing workspace name when not set"""
        config = CLIConfig()

        with pytest.raises(ValueError, match="No active workspace name"):
            _ = config.active_workspace_name
