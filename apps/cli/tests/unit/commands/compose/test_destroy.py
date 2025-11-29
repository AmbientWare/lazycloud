"""Unit tests for compose destroy command

Note: These tests focus on validation, confirmation logic, and error handling.
Full destroy flow with async operations is better suited for E2E tests.
"""

import pytest
import typer
from models.statuses import TaskStatus

from cli.commands.compose.destroy import destroy


@pytest.mark.unit
class TestDestroy:
    """Tests for destroy command"""

    def test_destroy_requires_deployment_name_or_lazycloud_file(self, mocker):
        """Test error when no deployment name and no lazycloud file"""
        mock_get_name = mocker.patch(
            "cli.commands.compose.destroy.get_current_deployment_name"
        )
        mock_get_name.return_value = None

        mocker.patch("cli.commands.compose.destroy.console")
        mock_view = mocker.patch("cli.commands.compose.destroy.DestroyView")

        with pytest.raises(typer.Exit) as exc_info:
            destroy(name=None, force=False)

        assert exc_info.value.exit_code == 1
        mock_view.return_value.show_error.assert_called_once()

    def test_destroy_not_found(self, mocker):
        """Test error when deployment not found"""
        mock_api = mocker.patch("cli.commands.compose.destroy.api")
        mock_api.deployments.get_deployment.return_value = None

        mocker.patch("cli.commands.compose.destroy.console")
        mock_view = mocker.patch("cli.commands.compose.destroy.DestroyView")

        with pytest.raises(typer.Exit) as exc_info:
            destroy(name="nonexistent", force=False)

        assert exc_info.value.exit_code == 1
        mock_view.return_value.show_error.assert_called()

    def test_destroy_cancelled_by_user(self, mocker):
        """Test cancellation when user declines confirmation"""
        mock_api = mocker.patch("cli.commands.compose.destroy.api")
        mock_deployment = mocker.Mock()
        mock_deployment.id = "dep123"
        mock_deployment.name = "test-deployment"
        mock_api.deployments.get_deployment.return_value = mock_deployment

        mocker.patch("cli.commands.compose.destroy.console")
        mock_view = mocker.patch("cli.commands.compose.destroy.DestroyView")
        mock_view.return_value.confirm_destruction.return_value = False

        # Should not raise Exit when user cancels
        destroy(name="test-deployment", force=False)

        mock_view.return_value.show_cancelled.assert_called_once()
        mock_api.deployments.delete_deployment.assert_not_called()

    def test_destroy_success(self, mocker):
        """Test successful destruction"""
        mock_api = mocker.patch("cli.commands.compose.destroy.api")
        mock_deployment = mocker.Mock()
        mock_deployment.id = "dep123"
        mock_deployment.name = "test-deployment"
        mock_api.deployments.get_deployment.return_value = mock_deployment

        # Mock async delete as a coroutine
        async def mock_delete(deployment_id):
            mock_response = mocker.Mock()
            mock_response.status = TaskStatus.COMPLETED
            return mock_response

        mock_api.deployments.delete_deployment = mock_delete

        mocker.patch("cli.commands.compose.destroy.console")
        mock_view = mocker.patch("cli.commands.compose.destroy.DestroyView")
        mock_view.return_value.confirm_destruction.return_value = True

        destroy(name="test-deployment", force=False)

        mock_view.return_value.show_success.assert_called_once_with("test-deployment")

    def test_destroy_with_force_flag(self, mocker):
        """Test force flag is passed to confirmation handler"""
        mock_api = mocker.patch("cli.commands.compose.destroy.api")
        mock_deployment = mocker.Mock()
        mock_deployment.id = "dep123"
        mock_deployment.name = "test-deployment"
        mock_api.deployments.get_deployment.return_value = mock_deployment

        # Mock async delete as a coroutine
        async def mock_delete(deployment_id):
            mock_response = mocker.Mock()
            mock_response.status = TaskStatus.COMPLETED
            return mock_response

        mock_api.deployments.delete_deployment = mock_delete

        mocker.patch("cli.commands.compose.destroy.console")
        mock_view = mocker.patch("cli.commands.compose.destroy.DestroyView")
        mock_view.return_value.confirm_destruction.return_value = True

        destroy(name="test-deployment", force=True)

        # Confirmation should be called with force=True
        mock_view.return_value.confirm_destruction.assert_called_once_with(
            "test-deployment", True
        )

    def test_destroy_failure(self, mocker):
        """Test destruction failure handling"""
        mock_api = mocker.patch("cli.commands.compose.destroy.api")
        mock_deployment = mocker.Mock()
        mock_deployment.id = "dep123"
        mock_deployment.name = "test-deployment"
        mock_api.deployments.get_deployment.return_value = mock_deployment

        # Mock async delete as a coroutine
        async def mock_delete(deployment_id):
            mock_response = mocker.Mock()
            mock_response.status = TaskStatus.ERROR
            mock_response.message = "Deletion failed"
            return mock_response

        mock_api.deployments.delete_deployment = mock_delete

        mocker.patch("cli.commands.compose.destroy.console")
        mock_view = mocker.patch("cli.commands.compose.destroy.DestroyView")
        mock_view.return_value.confirm_destruction.return_value = True

        with pytest.raises(typer.Exit) as exc_info:
            destroy(name="test-deployment", force=False)

        assert exc_info.value.exit_code == 1
        mock_view.return_value.show_error.assert_called()
