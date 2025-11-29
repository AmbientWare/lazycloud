"""Unit tests for workspace commands"""

import pytest
from cli.commands.workspaces import workspace_app
from typer.testing import CliRunner

runner = CliRunner()


@pytest.mark.unit
class TestWorkspacesList:
    """Tests for workspaces list command"""

    def test_list_workspaces_success(self, mock_home_dir, sample_workspaces, mocker):
        """Test listing workspaces successfully"""
        mock_api = mocker.patch("cli.commands.workspaces.list.api")
        mock_api.workspaces.list_workspaces.return_value = sample_workspaces

        # Mock config to have an active workspace
        mock_config = mocker.patch("cli.commands.workspaces.list.config")
        mock_config.active_workspace_id = "ws_123456"

        result = runner.invoke(workspace_app, ["list"])

        assert result.exit_code == 0
        mock_api.workspaces.list_workspaces.assert_called_once()

    def test_list_workspaces_empty(self, mock_home_dir, mocker):
        """Test listing when no workspaces exist"""
        mock_api = mocker.patch("cli.commands.workspaces.list.api")
        mock_api.workspaces.list_workspaces.return_value = []

        result = runner.invoke(workspace_app, ["list"])

        assert result.exit_code == 0
        mock_api.workspaces.list_workspaces.assert_called_once()

    def test_list_workspaces_api_error(self, mock_home_dir, mocker):
        """Test list handles API errors"""
        mock_api = mocker.patch("cli.commands.workspaces.list.api")
        mock_api.workspaces.list_workspaces.side_effect = Exception("API Error")

        result = runner.invoke(workspace_app, ["list"])

        assert result.exit_code == 1


@pytest.mark.unit
class TestWorkspacesCreate:
    """Tests for workspaces create command"""

    def test_create_workspace_success(self, mock_home_dir, mocker):
        """Test creating a workspace successfully"""
        mock_api = mocker.patch("cli.commands.workspaces.create.api")
        mock_api.workspaces.create_workspace.return_value = {
            "id": "ws_new123",
            "name": "New Workspace",
        }

        # Mock the view to avoid rich output
        mocker.patch("cli.commands.workspaces.create.WorkspaceView")

        result = runner.invoke(workspace_app, ["create", "New Workspace"])

        assert result.exit_code == 0
        mock_api.workspaces.create_workspace.assert_called_once_with("New Workspace")

    def test_create_workspace_api_error(self, mock_home_dir, mocker):
        """Test create handles API errors"""
        mock_api = mocker.patch("cli.commands.workspaces.create.api")
        mock_api.workspaces.create_workspace.side_effect = Exception("API Error")

        mocker.patch("cli.commands.workspaces.create.WorkspaceView")

        result = runner.invoke(workspace_app, ["create", "New Workspace"])

        assert result.exit_code == 1

    def test_create_workspace_requires_name(self, mock_home_dir):
        """Test create requires workspace name argument"""
        result = runner.invoke(workspace_app, ["create"])

        assert result.exit_code != 0


@pytest.mark.unit
class TestWorkspacesActivate:
    """Tests for workspaces activate command"""

    def test_activate_workspace_success(self, mock_home_dir, mocker):
        """Test activating a workspace successfully"""
        workspace_data = {"id": "ws_123", "name": "Test Workspace"}

        mock_api = mocker.patch("cli.commands.workspaces.activate.api")
        mock_api.workspaces.get_workspace_by_name.return_value = workspace_data

        mock_config = mocker.patch("cli.commands.workspaces.activate.config")
        mocker.patch("cli.commands.workspaces.activate.WorkspaceView")

        result = runner.invoke(workspace_app, ["activate", "Test Workspace"])

        assert result.exit_code == 0
        mock_api.workspaces.get_workspace_by_name.assert_called_once_with(
            "Test Workspace"
        )
        mock_config.set_active_workspace.assert_called_once_with(
            "ws_123", "Test Workspace"
        )

    def test_activate_nonexistent_workspace(self, mock_home_dir, mocker):
        """Test activating a workspace that doesn't exist"""
        mock_api = mocker.patch("cli.commands.workspaces.activate.api")
        mock_api.workspaces.get_workspace_by_name.return_value = None

        mocker.patch("cli.commands.workspaces.activate.WorkspaceView")

        result = runner.invoke(workspace_app, ["activate", "Nonexistent"])

        assert result.exit_code == 1

    def test_activate_workspace_api_error(self, mock_home_dir, mocker):
        """Test activate handles API errors"""
        mock_api = mocker.patch("cli.commands.workspaces.activate.api")
        mock_api.workspaces.get_workspace_by_name.side_effect = Exception("API Error")

        mocker.patch("cli.commands.workspaces.activate.WorkspaceView")

        result = runner.invoke(workspace_app, ["activate", "Test"])

        assert result.exit_code == 1

    def test_activate_requires_name(self, mock_home_dir):
        """Test activate requires workspace name argument"""
        result = runner.invoke(workspace_app, ["activate"])

        assert result.exit_code != 0
