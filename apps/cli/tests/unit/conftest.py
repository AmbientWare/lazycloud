"""Unit test fixtures and helpers"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_home_dir(tmp_path, monkeypatch):
    """Mock the home directory to use a temporary directory"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture
def mock_config_file(mock_home_dir):
    """Create a mock .lazycloud config file"""
    config_file = mock_home_dir / ".lazycloud"

    def _create_config(api_key=None, workspace_id=None, workspace_name=None):
        lines = []
        if api_key:
            lines.append(f"API_KEY={api_key}")
        if workspace_id:
            lines.append(f"ACTIVE_WORKSPACE_ID={workspace_id}")
        if workspace_name:
            lines.append(f"ACTIVE_WORKSPACE_NAME={workspace_name}")

        config_file.write_text("\n".join(lines) + "\n" if lines else "")
        return config_file

    return _create_config


@pytest.fixture
def mock_api_client(mocker):
    """Mock the API client"""
    mock_client = MagicMock()

    # Mock workspaces API
    mock_client.workspaces = MagicMock()
    mock_client.workspaces.list_workspaces = MagicMock(return_value=[])

    # Mock deployments API
    mock_client.deployments = MagicMock()
    mock_client.deployments.list_deployments = MagicMock(return_value=[])

    return mock_client


@pytest.fixture
def sample_workspace():
    """Sample workspace data"""
    return {
        "id": "ws_123456",
        "name": "My Workspace",
        "is_personal": True,
        "role": "owner",
    }


@pytest.fixture
def sample_workspaces(sample_workspace):
    """Sample workspaces list"""
    return [
        sample_workspace,
        {
            "id": "ws_789012",
            "name": "Team Workspace",
            "is_personal": False,
            "role": "member",
        },
    ]


@pytest.fixture
def sample_deployment():
    """Sample deployment data"""
    return {
        "id": "dep_123456",
        "name": "my-app",
        "status": "running",
        "revision": 1,
        "services": [
            {
                "name": "web",
                "status": "running",
                "replicas": 1,
            }
        ],
    }


@pytest.fixture
def mock_env_vars(monkeypatch):
    """Helper to set environment variables for tests"""

    def _set_env(**kwargs):
        for key, value in kwargs.items():
            monkeypatch.setenv(key, value)

    return _set_env


@pytest.fixture
def clear_env_vars(monkeypatch):
    """Clear LazyCloud environment variables"""
    env_vars = [
        "LAZYCLOUD_API_KEY",
        "LAZYCLOUD_WORKSPACE_ID",
        "LAZYCLOUD_API_BASE_URL",
    ]
    for var in env_vars:
        monkeypatch.delenv(var, raising=False)
