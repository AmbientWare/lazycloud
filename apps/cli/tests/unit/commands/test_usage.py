"""Unit tests for usage command"""

import pytest
from typer.testing import CliRunner

from cli.commands import main_cli

runner = CliRunner()


@pytest.mark.unit
class TestUsage:
    """Tests for usage command"""

    def test_usage_launches_dashboard(self, mock_config_dir, mocker):
        """Test usage command launches the usage dashboard"""
        # Mock the authentication check in main CLI
        mock_config = mocker.patch("cli.commands.config")
        mock_config.check_authentication.return_value = (True, "")

        # Mock the usage UI
        mock_run_usage = mocker.patch("cli.commands.usage.run_usage")

        result = runner.invoke(main_cli, ["usage"])

        assert result.exit_code == 0
        mock_run_usage.assert_called_once()
