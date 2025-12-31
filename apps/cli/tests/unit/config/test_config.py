"""Unit tests for CLI configuration"""

import cli.config
import pytest
from cli.config import CLIConfig


@pytest.mark.unit
class TestCLIConfig:
    """Tests for CLIConfig class"""

    def test_config_from_env_vars(self, mock_config_dir, monkeypatch):
        """Test configuration from environment variables"""
        monkeypatch.setenv("LAZYCLOUD_API_BASE_URL", "https://custom.api.dev")
        monkeypatch.setenv("LAZYCLOUD_API_VERSION", "v2")

        config = CLIConfig()

        assert config.api_base_url == "https://custom.api.dev"
        assert config.api_version == "v2"
        assert config.api_url == "https://custom.api.dev/v2"

    def test_api_base_url_validation_invalid(self, mock_config_dir):
        """Test API base URL validation rejects invalid URLs"""
        with pytest.raises(ValueError, match="must start with http"):
            CLIConfig(api_base_url="example.com")

    def test_api_base_url_strips_trailing_slash(self, mock_config_dir):
        """Test that trailing slashes are removed from base URL"""
        config = CLIConfig(api_base_url="http://localhost:8000/")
        assert config.api_base_url == "http://localhost:8000"

    def test_load_config_from_file(self, mock_config_file):
        """Test loading configuration from file"""
        mock_config_file(
            access_token="test_token_123",
            workspace_id="ws_123",
            workspace_name="My Workspace",
        )

        config = CLIConfig()

        assert config.access_token == "test_token_123"
        assert config.active_workspace_id == "ws_123"
        assert config.active_workspace_name == "My Workspace"

    def test_set_tokens(self, mock_config_dir):
        """Test setting access tokens"""
        config = CLIConfig()
        config.set_tokens("new_token_456", "refresh_token_789")

        assert config.access_token == "new_token_456"
        assert config.refresh_token == "refresh_token_789"

        # Verify it's saved to file
        assert cli.config.CONFIG_FILE.exists()
        content = cli.config.CONFIG_FILE.read_text()
        assert 'access_token = "new_token_456"' in content
        assert 'refresh_token = "refresh_token_789"' in content

    def test_clear_tokens(self, mock_config_file):
        """Test clearing tokens"""
        mock_config_file(access_token="test_token", refresh_token="refresh_token")

        config = CLIConfig()
        assert config.access_token == "test_token"

        config.clear_tokens()
        assert config.access_token is None
        assert config.refresh_token is None

    def test_access_token_from_env_var(self, mock_config_dir, monkeypatch):
        """Test access token from LAZYCLOUD_API_KEY environment variable"""
        monkeypatch.setenv("LAZYCLOUD_API_KEY", "env_key_789")

        config = CLIConfig()
        assert config.access_token == "env_key_789"

    def test_access_token_env_priority_over_file(self, mock_config_file, monkeypatch):
        """Test that env var API key takes priority over stored token"""
        mock_config_file(access_token="file_token")
        monkeypatch.setenv("LAZYCLOUD_API_KEY", "env_key")

        config = CLIConfig()
        # Env var should take priority
        assert config.access_token == "env_key"

    def test_set_active_workspace(self, mock_config_dir):
        """Test setting active workspace"""
        config = CLIConfig()
        config.set_active_workspace("ws_123", "Test Workspace")

        assert config.active_workspace_id == "ws_123"
        assert config.active_workspace_name == "Test Workspace"

        # Verify it's saved to file
        content = cli.config.CONFIG_FILE.read_text()
        assert 'id = "ws_123"' in content
        assert 'name = "Test Workspace"' in content

    def test_clear_active_workspace(self, mock_config_file):
        """Test clearing active workspace"""
        mock_config_file(workspace_id="ws_123", workspace_name="Test")

        config = CLIConfig()
        config.clear_active_workspace()

        with pytest.raises(ValueError, match="No active workspace"):
            _ = config.active_workspace_id

    def test_active_workspace_from_env_var(self, mock_config_dir, monkeypatch):
        """Test active workspace from environment variable"""
        monkeypatch.setenv("LAZYCLOUD_WORKSPACE_ID", "ws_env")

        config = CLIConfig()
        assert config.active_workspace_id == "ws_env"

    def test_active_workspace_file_priority_over_env(
        self, mock_config_file, monkeypatch
    ):
        """Test that stored workspace takes priority over env var"""
        mock_config_file(workspace_id="ws_file")
        monkeypatch.setenv("LAZYCLOUD_WORKSPACE_ID", "ws_env")

        config = CLIConfig()
        assert config.active_workspace_id == "ws_file"

    def test_check_authentication_not_logged_in(self, mock_config_dir):
        """Test authentication check when not logged in"""
        config = CLIConfig()
        is_authed, message = config.check_authentication()

        assert is_authed is False
        assert "Not logged in" in message
        assert "lazycloud login" in message

    def test_check_authentication_no_workspace(self, mock_config_file):
        """Test authentication check when token set but no workspace"""
        mock_config_file(access_token="test_token")

        config = CLIConfig()
        is_authed, message = config.check_authentication()

        assert is_authed is False
        assert "No workspace configured" in message

    def test_check_authentication_success(self, mock_config_file):
        """Test authentication check when fully configured"""
        mock_config_file(
            access_token="test_token", workspace_id="ws_123", workspace_name="Test"
        )

        config = CLIConfig()
        is_authed, message = config.check_authentication()

        assert is_authed is True
        assert message == ""

    def test_check_authentication_env_var_only(self, mock_config_dir, monkeypatch):
        """Test authentication check with env var only (CI/CD mode)"""
        monkeypatch.setenv("LAZYCLOUD_API_KEY", "env_key")
        monkeypatch.setenv("LAZYCLOUD_WORKSPACE_ID", "ws_env")

        config = CLIConfig()
        is_authed, message = config.check_authentication()

        # Should be valid for CI/CD usage
        assert is_authed is True
        assert message == ""

    def test_active_workspace_name_no_value(self, mock_config_dir):
        """Test accessing workspace name when not set"""
        config = CLIConfig()

        with pytest.raises(ValueError, match="No active workspace name"):
            _ = config.active_workspace_name

    def test_config_loads_api_settings_from_file(self, mock_config_file):
        """Test that API settings are loaded from config file"""
        mock_config_file(api_base_url="http://localhost:9000")

        config = CLIConfig()
        assert config.api_base_url == "http://localhost:9000"

    def test_default_values(self, mock_config_dir):
        """Test default configuration values"""
        config = CLIConfig()

        assert config.api_base_url == "https://api.lazycloud.dev"
        assert config.api_version == "v1"
