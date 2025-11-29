"""Unit tests for dashboard command

Note: The dashboard command launches a Textual TUI app which is difficult to
unit test meaningfully. These tests focus on auth checks and error handling.
Full dashboard UI behavior is better suited for E2E tests.
"""

import pytest
import typer

from cli.commands.dashboard import dashboard


@pytest.mark.unit
class TestDashboard:
    """Tests for dashboard command"""

    def test_dashboard_requires_authentication(self, mock_home_dir, mocker):
        """Test dashboard function checks authentication before launching"""
        # Mock console and config
        mock_console = mocker.patch("cli.commands.dashboard.console")
        mock_config = mocker.patch("cli.commands.dashboard.config")
        mock_config.check_authentication.return_value = (False, "Not logged in")

        # Should raise Exit(1) when not authenticated
        with pytest.raises(typer.Exit) as exc_info:
            dashboard()

        assert exc_info.value.exit_code == 1
        # Verify error was printed
        mock_console.print.assert_called_once()

    def test_dashboard_launches_when_authenticated(self, mock_config_file, mocker):
        """Test dashboard launches when properly authenticated"""
        mock_config_file(
            api_key="test_key",
            workspace_id="ws_123",
            workspace_name="Test Workspace",
        )

        # Mock the dashboard UI
        mock_run_dashboard = mocker.patch("cli.commands.dashboard.run_dashboard")

        dashboard()

        mock_run_dashboard.assert_called_once()

    def test_dashboard_keyboard_interrupt(self, mock_config_file, mocker):
        """Test dashboard handles keyboard interrupt gracefully"""
        mock_config_file(
            api_key="test_key",
            workspace_id="ws_123",
            workspace_name="Test Workspace",
        )

        # Mock dashboard to raise KeyboardInterrupt
        mock_run_dashboard = mocker.patch("cli.commands.dashboard.run_dashboard")
        mock_run_dashboard.side_effect = KeyboardInterrupt()

        # Should not raise - keyboard interrupt is handled
        dashboard()

    def test_dashboard_connection_error(self, mock_config_file, mocker):
        """Test dashboard handles connection errors"""
        mock_config_file(
            api_key="test_key",
            workspace_id="ws_123",
            workspace_name="Test Workspace",
        )

        # Mock console and dashboard
        mock_console = mocker.patch("cli.commands.dashboard.console")
        mock_run_dashboard = mocker.patch("cli.commands.dashboard.run_dashboard")
        mock_run_dashboard.side_effect = ConnectionError("Connection refused")

        with pytest.raises(typer.Exit) as exc_info:
            dashboard()

        assert exc_info.value.exit_code == 1
        # Verify error was printed
        mock_console.print.assert_called_once()

    def test_dashboard_generic_error(self, mock_config_file, mocker):
        """Test dashboard handles generic errors"""
        mock_config_file(
            api_key="test_key",
            workspace_id="ws_123",
            workspace_name="Test Workspace",
        )

        # Mock console and dashboard
        mock_console = mocker.patch("cli.commands.dashboard.console")
        mock_run_dashboard = mocker.patch("cli.commands.dashboard.run_dashboard")
        mock_run_dashboard.side_effect = Exception("Something went wrong")

        with pytest.raises(typer.Exit) as exc_info:
            dashboard()

        assert exc_info.value.exit_code == 1
        # Verify error was printed
        mock_console.print.assert_called_once()
