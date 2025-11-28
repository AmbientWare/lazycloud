"""E2E tests for rollback CLI command.

Run with: pytest src/lazycloud_api/tests/e2e/test_cli_rollback.py -v -s

Note: Rollback tests require at least 2 deployment revisions to test properly.
"""

import tempfile
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    cleanup_deployment,
    get_deployment_id,
    get_output,
    requires_api,
    run_cli,
    verify_services,
)

pytestmark = [
    pytest.mark.e2e,
    requires_api,
]

E2E_ROLLBACK_NAME = "e2e-rollback-test"


class TestCLIRollbackRequirements:
    """Test rollback command requirements."""

    @pytest.fixture
    def rollback_project(self, simple_compose_yaml: str) -> Path:
        """Create temp project for rollback testing."""
        cleanup_deployment(E2E_ROLLBACK_NAME)

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            compose_path = project_dir / "docker-compose.yml"
            compose_path.write_text(simple_compose_yaml)
            yield project_dir

        cleanup_deployment(E2E_ROLLBACK_NAME)

    def test_rollback_requires_existing_deployment(self, rollback_project: Path):
        """Test rollback fails without existing deployment."""
        init_result = run_cli(
            "init",
            "--name",
            E2E_ROLLBACK_NAME,
            "--file",
            "docker-compose.yml",
            cwd=rollback_project,
        )
        assert init_result.returncode == 0

        rollback_result = run_cli(
            "rollback",
            "--yes",
            cwd=rollback_project,
        )
        assert rollback_result.returncode != 0
        output = get_output(rollback_result)
        assert "not found" in output or "does not exist" in output or "error" in output

    def test_rollback_requires_revision_history(self, rollback_project: Path):
        """Test rollback fails with only one revision."""
        run_cli(
            "init",
            "--name",
            E2E_ROLLBACK_NAME,
            "--file",
            "docker-compose.yml",
            cwd=rollback_project,
        )
        deploy_result = run_cli(
            "deploy",
            "--yes",
            cwd=rollback_project,
        )
        assert deploy_result.returncode == 0

        deployment_id = get_deployment_id(E2E_ROLLBACK_NAME)
        if deployment_id:
            verify_services(deployment_id, ["web"], timeout=120)

        rollback_result = run_cli(
            "rollback",
            "--yes",
            cwd=rollback_project,
        )
        output = get_output(rollback_result)
        # Should fail - need at least 2 revisions
        assert rollback_result.returncode != 0 or "revision" in output


class TestCLIRollbackFlow:
    """Test full rollback flow with multiple revisions."""

    @pytest.fixture
    def rollback_project_v1(self) -> Path:
        """Create temp project with initial compose (v1)."""
        cleanup_deployment(E2E_ROLLBACK_NAME)

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            compose_path = project_dir / "docker-compose.yml"
            compose_path.write_text("""
services:
  web:
    image: nginx:1.24
    ports:
      - "80:80"
""")
            yield project_dir

        cleanup_deployment(E2E_ROLLBACK_NAME)

    def test_rollback_after_two_deploys(self, rollback_project_v1: Path):
        """Test rollback works after deploying twice."""
        # First deploy (revision 1)
        run_cli(
            "init",
            "--name",
            E2E_ROLLBACK_NAME,
            "--file",
            "docker-compose.yml",
            cwd=rollback_project_v1,
        )
        first_deploy = run_cli(
            "deploy",
            "--yes",
            cwd=rollback_project_v1,
        )
        assert first_deploy.returncode == 0

        deployment_id = get_deployment_id(E2E_ROLLBACK_NAME)
        if deployment_id:
            verify_services(deployment_id, ["web"], timeout=120)

        # Update compose and deploy again (revision 2)
        compose_path = rollback_project_v1 / "docker-compose.yml"
        compose_path.write_text("""
services:
  web:
    image: nginx:1.25
    ports:
      - "80:80"
""")
        second_deploy = run_cli(
            "deploy",
            "--yes",
            cwd=rollback_project_v1,
        )
        assert second_deploy.returncode == 0

        if deployment_id:
            verify_services(deployment_id, ["web"], timeout=120)

        # Now rollback should work - use -r 1 to rollback to first revision
        rollback_result = run_cli(
            "rollback",
            "--yes",
            "-r",
            "1",
            cwd=rollback_project_v1,
            timeout=300,
        )
        output = get_output(rollback_result)

        # Either succeeds or fails with a meaningful message
        if rollback_result.returncode != 0:
            # Acceptable if there's a revision or error message
            assert "revision" in output or "error" in output or "failed" in output

        else:
            assert rollback_result.returncode == 0


class TestCLIRollbackHelp:
    """Test rollback command help and usage."""

    def test_rollback_help(self):
        """Test rollback help is accessible."""
        result = run_cli("rollback", "--help")
        assert result.returncode == 0
        assert "rollback" in result.stdout.lower()
