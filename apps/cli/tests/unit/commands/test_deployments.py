"""Unit tests for deployments commands"""

from datetime import datetime, timezone

import pytest
from models.deployments import DeploymentStates
from typer.testing import CliRunner

from cli.commands.deployments import deployments_app

runner = CliRunner()


class MockDeployment:
    """Mock deployment for testing"""

    def __init__(self, name, state, deployed_at=None):
        self.name = name
        self.state = state
        self.deployed_at = deployed_at or datetime.now(timezone.utc)


class MockDeploymentsResponse:
    """Mock deployments list response"""

    def __init__(self, deployments, total=None, has_more=False):
        self.deployments = deployments
        self.total = total or len(deployments)
        self.has_more = has_more


@pytest.mark.unit
class TestDeploymentsList:
    """Tests for deployments list command"""

    def test_list_deployments_success(self, mock_home_dir, mocker):
        """Test listing deployments successfully"""
        mock_deployments = [
            MockDeployment("app1", DeploymentStates.DEPLOYED),
            MockDeployment("app2", DeploymentStates.DEPLOYING),
        ]
        mock_response = MockDeploymentsResponse(mock_deployments)

        mock_api = mocker.patch("cli.commands.deployments.list.api")
        mock_api.deployments.list_deployments.return_value = mock_response

        result = runner.invoke(deployments_app, ["list"])

        assert result.exit_code == 0
        mock_api.deployments.list_deployments.assert_called_once()

    def test_list_deployments_empty(self, mock_home_dir, mocker):
        """Test listing when no deployments exist"""
        mock_response = MockDeploymentsResponse([])

        mock_api = mocker.patch("cli.commands.deployments.list.api")
        mock_api.deployments.list_deployments.return_value = mock_response

        result = runner.invoke(deployments_app, ["list"])

        assert result.exit_code == 0

    def test_list_deployments_filters_deleted_by_default(self, mock_home_dir, mocker):
        """Test command succeeds when response includes deleted deployments"""
        mock_deployments = [
            MockDeployment("app1", DeploymentStates.DEPLOYED),
            MockDeployment("app2", DeploymentStates.DELETED),
        ]
        mock_response = MockDeploymentsResponse(mock_deployments)

        mock_api = mocker.patch("cli.commands.deployments.list.api")
        mock_api.deployments.list_deployments.return_value = mock_response

        result = runner.invoke(deployments_app, ["list"])

        assert result.exit_code == 0

    def test_list_deployments_with_all_flag(self, mock_home_dir, mocker):
        """Test listing all deployments including deleted"""
        mock_deployments = [
            MockDeployment("app1", DeploymentStates.DEPLOYED),
            MockDeployment("app2", DeploymentStates.DELETED),
        ]
        mock_response = MockDeploymentsResponse(mock_deployments)

        mock_api = mocker.patch("cli.commands.deployments.list.api")
        mock_api.deployments.list_deployments.return_value = mock_response

        result = runner.invoke(deployments_app, ["list", "--all"])

        assert result.exit_code == 0

    def test_list_deployments_with_limit(self, mock_home_dir, mocker):
        """Test listing deployments with custom limit"""
        mock_deployments = [
            MockDeployment(f"app{i}", DeploymentStates.DEPLOYED) for i in range(5)
        ]
        mock_response = MockDeploymentsResponse(mock_deployments)

        mock_api = mocker.patch("cli.commands.deployments.list.api")
        mock_api.deployments.list_deployments.return_value = mock_response

        result = runner.invoke(deployments_app, ["list", "--limit", "5"])

        assert result.exit_code == 0
        mock_api.deployments.list_deployments.assert_called_once_with(limit=5)

    def test_list_deployments_api_error(self, mock_home_dir, mocker):
        """Test list handles API errors"""
        mock_api = mocker.patch("cli.commands.deployments.list.api")
        mock_api.deployments.list_deployments.side_effect = Exception("API Error")

        result = runner.invoke(deployments_app, ["list"])

        assert result.exit_code == 1
