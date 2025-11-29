"""Unit tests for compose init command

Note: These tests focus on validation logic and error handling.
Full init flow (file creation, compose file detection) is better suited for E2E tests.
"""

import pytest
import typer

from cli.commands.compose.init import (
    find_compose_files,
    suggest_deployment_name,
    validate_deployment_name,
)


@pytest.mark.unit
class TestValidateDeploymentName:
    """Tests for deployment name validation"""

    def test_valid_deployment_name(self, mocker):
        """Test valid deployment name passes validation"""
        # Mock API to return no existing deployment
        mock_api = mocker.patch("cli.commands.compose.init.api")
        mock_api.deployments.get_deployment.return_value = None

        name, is_sync = validate_deployment_name("my-app")

        assert name == "my-app"
        assert is_sync is False

    def test_invalid_deployment_name_uppercase(self, mocker):
        """Test uppercase letters are rejected"""
        mocker.patch("cli.commands.compose.init.api")

        with pytest.raises(typer.BadParameter, match="lowercase"):
            validate_deployment_name("MyApp")

    def test_invalid_deployment_name_special_chars(self, mocker):
        """Test special characters are rejected"""
        mocker.patch("cli.commands.compose.init.api")

        with pytest.raises(typer.BadParameter, match="lowercase alphanumeric"):
            validate_deployment_name("my_app")

    def test_invalid_deployment_name_too_long(self, mocker):
        """Test names over 63 characters are rejected"""
        mocker.patch("cli.commands.compose.init.api")

        long_name = "a" * 64
        with pytest.raises(typer.BadParameter, match="63 characters or less"):
            validate_deployment_name(long_name)

    def test_invalid_deployment_name_starts_with_hyphen(self, mocker):
        """Test names starting with hyphen are rejected"""
        mocker.patch("cli.commands.compose.init.api")

        with pytest.raises(typer.BadParameter):
            validate_deployment_name("-myapp")

    def test_existing_deployment_sync_accepted(self, mocker):
        """Test syncing existing deployment when user confirms"""
        # Mock API to return existing deployment
        mock_api = mocker.patch("cli.commands.compose.init.api")
        mock_deployment = mocker.Mock()
        mock_deployment.namespace = "default"
        mock_deployment.state = mocker.Mock(value="running")
        mock_api.deployments.get_deployment.return_value = mock_deployment

        # Mock dialog to return True (user confirms sync)
        mock_dialog = mocker.patch("cli.commands.compose.init.SimpleConfirmationDialog")
        mock_dialog.return_value.show.return_value = True

        name, is_sync = validate_deployment_name("existing-app")

        assert name == "existing-app"
        assert is_sync is True

    def test_existing_deployment_sync_rejected(self, mocker):
        """Test rejecting sync of existing deployment"""
        # Mock API to return existing deployment
        mock_api = mocker.patch("cli.commands.compose.init.api")
        mock_deployment = mocker.Mock()
        mock_api.deployments.get_deployment.return_value = mock_deployment

        # Mock dialog to return False (user rejects sync)
        mock_dialog = mocker.patch("cli.commands.compose.init.SimpleConfirmationDialog")
        mock_dialog.return_value.show.return_value = False

        with pytest.raises(typer.BadParameter, match="already exists"):
            validate_deployment_name("existing-app")


@pytest.mark.unit
class TestFindComposeFiles:
    """Tests for compose file detection"""

    def test_finds_docker_compose_yml(self, tmp_path, monkeypatch):
        """Test finding docker-compose.yml"""
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("version: '3'")

        monkeypatch.chdir(tmp_path)
        files = find_compose_files(tmp_path)

        assert "docker-compose.yml" in files

    def test_finds_compose_yaml(self, tmp_path, monkeypatch):
        """Test finding compose.yaml"""
        compose_file = tmp_path / "compose.yaml"
        compose_file.write_text("version: '3'")

        monkeypatch.chdir(tmp_path)
        files = find_compose_files(tmp_path)

        assert "compose.yaml" in files

    def test_no_compose_files(self, tmp_path, monkeypatch):
        """Test when no compose files exist"""
        monkeypatch.chdir(tmp_path)
        files = find_compose_files(tmp_path)

        assert len(files) == 0


@pytest.mark.unit
class TestSuggestDeploymentName:
    """Tests for deployment name suggestion"""

    def test_suggest_from_directory_name(self, tmp_path, monkeypatch):
        """Test suggesting name from directory"""
        test_dir = tmp_path / "my-app"
        test_dir.mkdir()
        monkeypatch.chdir(test_dir)

        name = suggest_deployment_name(test_dir)

        assert name == "my-app"

    def test_suggest_replaces_underscores(self, tmp_path, monkeypatch):
        """Test replacing underscores with hyphens"""
        test_dir = tmp_path / "my_app"
        test_dir.mkdir()
        monkeypatch.chdir(test_dir)

        name = suggest_deployment_name(test_dir)

        assert name == "my-app"

    def test_suggest_handles_uppercase(self, tmp_path, monkeypatch):
        """Test converting uppercase to lowercase"""
        test_dir = tmp_path / "MyApp"
        test_dir.mkdir()
        monkeypatch.chdir(test_dir)

        name = suggest_deployment_name(test_dir)

        assert name == "myapp"

    def test_suggest_prepends_app_for_digit_start(self, tmp_path, monkeypatch):
        """Test prepending 'app-' when name starts with digit"""
        test_dir = tmp_path / "123app"
        test_dir.mkdir()
        monkeypatch.chdir(test_dir)

        name = suggest_deployment_name(test_dir)

        assert name == "app-123app"

    def test_suggest_truncates_long_names(self, tmp_path, monkeypatch):
        """Test truncating names over 63 characters"""
        long_name = "a" * 70
        test_dir = tmp_path / long_name
        test_dir.mkdir()
        monkeypatch.chdir(test_dir)

        name = suggest_deployment_name(test_dir)

        assert len(name) <= 63
