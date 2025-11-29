"""Unit tests for compose rollback command

Note: These tests focus on validation, revision selection, and error handling.
Full rollback flow with async operations is better suited for E2E tests.
"""

from pathlib import Path

import pytest
import typer
from models.statuses import TaskStatus

from cli.commands.compose.rollback import rollback


@pytest.mark.unit
class TestRollback:
    """Tests for rollback command"""

    def test_rollback_requires_lazycloud_file(self, tmp_path, mocker, monkeypatch):
        """Test error when no lazycloud file found"""
        monkeypatch.chdir(tmp_path)

        mocker.patch("cli.commands.compose.rollback.console")
        mock_view = mocker.patch("cli.commands.compose.rollback.RollbackView")

        with pytest.raises(typer.Exit) as exc_info:
            rollback(yes=False, revision=None)

        assert exc_info.value.exit_code == 1
        mock_view.return_value.show_error.assert_called()

    def test_rollback_deployment_not_found(self, mocker):
        """Test error when deployment not found"""
        mock_api = mocker.patch("cli.commands.compose.rollback.api")
        mock_api.deployments.get_deployment.return_value = None

        mocker.patch("cli.commands.compose.rollback.console")
        mock_view = mocker.patch("cli.commands.compose.rollback.RollbackView")

        # Mock LazyCloudFile
        mock_file = mocker.patch("cli.commands.compose.rollback.LazyCloudFile")
        mock_instance = mocker.Mock()
        mock_config = mocker.Mock()
        mock_config.deployment_name = "test-app"
        mock_instance.read.return_value = mock_config
        mock_file.find_and_load.return_value = mock_instance

        with pytest.raises(typer.Exit) as exc_info:
            rollback(yes=False, revision=None)

        assert exc_info.value.exit_code == 1
        mock_view.return_value.show_error.assert_called()

    def test_rollback_insufficient_revisions(self, mocker):
        """Test error when not enough revision history"""
        mock_api = mocker.patch("cli.commands.compose.rollback.api")
        mock_deployment = mocker.Mock(id="dep123")
        mock_api.deployments.get_deployment.return_value = mock_deployment

        # Only 1 revision available
        mock_history = mocker.Mock()
        mock_history.revisions = [mocker.Mock(revision=1)]
        mock_api.deployments.get_deployment_history.return_value = mock_history

        mocker.patch("cli.commands.compose.rollback.console")
        mock_view = mocker.patch("cli.commands.compose.rollback.RollbackView")

        # Mock LazyCloudFile
        mock_file = mocker.patch("cli.commands.compose.rollback.LazyCloudFile")
        mock_instance = mocker.Mock()
        mock_config = mocker.Mock()
        mock_config.deployment_name = "test-app"
        mock_instance.read.return_value = mock_config
        mock_file.find_and_load.return_value = mock_instance

        with pytest.raises(typer.Exit) as exc_info:
            rollback(yes=False, revision=None)

        assert exc_info.value.exit_code == 1
        mock_view.return_value.show_error.assert_called()

    def test_rollback_invalid_revision_number(self, mocker):
        """Test error when invalid revision number specified"""
        mock_api = mocker.patch("cli.commands.compose.rollback.api")
        mock_deployment = mocker.Mock(id="dep123")
        mock_api.deployments.get_deployment.return_value = mock_deployment

        # Mock revision history
        mock_rev1 = mocker.Mock(revision=1)
        mock_rev2 = mocker.Mock(revision=2)
        mock_rev3 = mocker.Mock(revision=3)
        mock_history = mocker.Mock()
        mock_history.revisions = [mock_rev1, mock_rev2, mock_rev3]
        mock_api.deployments.get_deployment_history.return_value = mock_history

        mocker.patch("cli.commands.compose.rollback.console")
        mock_view = mocker.patch("cli.commands.compose.rollback.RollbackView")

        # Mock LazyCloudFile
        mock_file = mocker.patch("cli.commands.compose.rollback.LazyCloudFile")
        mock_instance = mocker.Mock()
        mock_config = mocker.Mock()
        mock_config.deployment_name = "test-app"
        mock_instance.read.return_value = mock_config
        mock_file.find_and_load.return_value = mock_instance

        # Try to rollback to invalid revision 5
        with pytest.raises(typer.Exit) as exc_info:
            rollback(yes=False, revision=5)

        assert exc_info.value.exit_code == 1
        mock_view.return_value.show_error.assert_called()

    def test_rollback_cancelled_by_user(self, mocker):
        """Test cancellation when user declines confirmation"""
        mock_api = mocker.patch("cli.commands.compose.rollback.api")
        mock_deployment = mocker.Mock(id="dep123")
        mock_api.deployments.get_deployment.return_value = mock_deployment

        # Mock revision history
        mock_rev1 = mocker.Mock(revision=1)
        mock_rev2 = mocker.Mock(revision=2)
        mock_rev3 = mocker.Mock(revision=3)
        mock_history = mocker.Mock()
        mock_history.revisions = [mock_rev1, mock_rev2, mock_rev3]
        mock_api.deployments.get_deployment_history.return_value = mock_history

        mocker.patch("cli.commands.compose.rollback.console")
        mock_view = mocker.patch("cli.commands.compose.rollback.RollbackView")
        mock_view.return_value.confirm_rollback.return_value = False

        # Mock LazyCloudFile
        mock_file = mocker.patch("cli.commands.compose.rollback.LazyCloudFile")
        mock_instance = mocker.Mock()
        mock_config = mocker.Mock()
        mock_config.deployment_name = "test-app"
        mock_instance.read.return_value = mock_config
        mock_file.find_and_load.return_value = mock_instance

        with pytest.raises(typer.Exit) as exc_info:
            rollback(yes=False, revision=1)

        assert exc_info.value.exit_code == 0
        mock_view.return_value.show_cancelled.assert_called()

    def test_rollback_success(self, mocker):
        """Test successful rollback"""
        mock_api = mocker.patch("cli.commands.compose.rollback.api")
        mock_deployment = mocker.Mock(id="dep123")
        mock_api.deployments.get_deployment.return_value = mock_deployment

        # Mock revision history
        mock_rev1 = mocker.Mock(revision=1)
        mock_rev2 = mocker.Mock(revision=2)
        mock_rev3 = mocker.Mock(revision=3)
        mock_history = mocker.Mock()
        mock_history.revisions = [mock_rev1, mock_rev2, mock_rev3]
        mock_api.deployments.get_deployment_history.return_value = mock_history

        # Mock async rollback as a coroutine
        async def mock_rollback(deployment_id, revision):
            mock_response = mocker.Mock()
            mock_response.status = TaskStatus.COMPLETED
            return mock_response

        mock_api.deployments.rollback_deployment = mock_rollback

        mocker.patch("cli.commands.compose.rollback.console")
        mock_view = mocker.patch("cli.commands.compose.rollback.RollbackView")
        mock_view.return_value.confirm_rollback.return_value = True

        # Mock LazyCloudFile
        mock_file = mocker.patch("cli.commands.compose.rollback.LazyCloudFile")
        mock_instance = mocker.Mock()
        mock_config = mocker.Mock()
        mock_config.deployment_name = "test-app"
        mock_instance.read.return_value = mock_config
        mock_file.find_and_load.return_value = mock_instance

        # Rollback to revision 1
        rollback(yes=False, revision=1)

        mock_view.return_value.show_success.assert_called()

    def test_rollback_with_yes_skips_confirmation(self, mocker):
        """Test --yes flag skips confirmation prompt"""
        mock_api = mocker.patch("cli.commands.compose.rollback.api")
        mock_deployment = mocker.Mock(id="dep123")
        mock_api.deployments.get_deployment.return_value = mock_deployment

        # Mock revision history
        mock_rev1 = mocker.Mock(revision=1)
        mock_rev2 = mocker.Mock(revision=2)
        mock_history = mocker.Mock()
        mock_history.revisions = [mock_rev1, mock_rev2]
        mock_api.deployments.get_deployment_history.return_value = mock_history

        # Mock async rollback as a coroutine
        async def mock_rollback(deployment_id, revision):
            mock_response = mocker.Mock()
            mock_response.status = TaskStatus.COMPLETED
            return mock_response

        mock_api.deployments.rollback_deployment = mock_rollback

        mocker.patch("cli.commands.compose.rollback.console")
        mock_view = mocker.patch("cli.commands.compose.rollback.RollbackView")

        # Mock LazyCloudFile
        mock_file = mocker.patch("cli.commands.compose.rollback.LazyCloudFile")
        mock_instance = mocker.Mock()
        mock_config = mocker.Mock()
        mock_config.deployment_name = "test-app"
        mock_instance.read.return_value = mock_config
        mock_file.find_and_load.return_value = mock_instance

        rollback(yes=True, revision=1)

        # Confirmation should not be called when yes=True
        mock_view.return_value.confirm_rollback.assert_not_called()
        mock_view.return_value.show_success.assert_called()
