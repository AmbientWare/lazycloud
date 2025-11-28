"""E2E tests for deployment CLI commands.

Requires:
    docker network create lazycloud
    uv run mk-up --fresh
    docker compose up -d

Run with: pytest src/lazycloud_api/tests/e2e/test_cli_deployments.py -v -s

Note: Tests use fixed deployment names and clean up before/after each test
to avoid hitting deployment limits.

Test order: Build test runs FIRST to avoid Prefect retry race conditions.
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

# Fixed deployment names to avoid accumulating deployments
E2E_DEPLOY_NAME = "e2e-test-deploy"
E2E_MULTI_NAME = "e2e-test-multi"
E2E_BUILD_NAME = "e2e-test-build"


class TestCLIBuildDeployment:
    """Test deployment with image build. Runs first to start with clean state."""

    @pytest.fixture
    def build_project(self, build_compose_yaml: str, simple_dockerfile: str) -> Path:
        """Create temp project with build context."""
        cleanup_deployment(E2E_BUILD_NAME)

        # Ensure Personal workspace is active (may have been cleared by previous tests)
        run_cli("workspaces", "activate", "Personal", verbose=False)

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            compose_path = project_dir / "docker-compose.yml"
            compose_path.write_text(build_compose_yaml)

            dockerfile_path = project_dir / "Dockerfile"
            dockerfile_path.write_text(simple_dockerfile)

            yield project_dir

        cleanup_deployment(E2E_BUILD_NAME)

    def test_build_deploy(self, build_project: Path):
        """Test deploying a service that requires building an image."""
        init_result = run_cli(
            "init",
            "--name",
            E2E_BUILD_NAME,
            "--file",
            "docker-compose.yml",
            cwd=build_project,
        )
        assert init_result.returncode == 0, f"Init failed: {get_output(init_result)}"

        deploy_result = run_cli(
            "deploy",
            "--yes",
            cwd=build_project,
            timeout=600,  # 10 min for builds
        )
        assert deploy_result.returncode == 0, (
            f"Build deploy failed: {get_output(deploy_result)}"
        )

        deployment_id = get_deployment_id(E2E_BUILD_NAME)
        if deployment_id:
            success, msg = verify_services(deployment_id, ["app"], timeout=180)
            assert success, f"K8s verification failed: {msg}"

        destroy_result = run_cli(
            "destroy",
            E2E_BUILD_NAME,
            "--force",
            cwd=build_project,
        )

        if destroy_result.returncode != 0:
            print(f"Warning: destroy failed: {get_output(destroy_result)}")


class TestCLIDeploymentFlow:
    """Test deployment lifecycle via CLI."""

    @pytest.fixture
    def temp_project(self, simple_compose_yaml: str) -> Path:
        """Create a temporary project directory with compose file."""
        cleanup_deployment(E2E_DEPLOY_NAME)

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            compose_path = project_dir / "docker-compose.yml"
            compose_path.write_text(simple_compose_yaml)

            yield project_dir

        cleanup_deployment(E2E_DEPLOY_NAME)

    def test_cli_init(self, temp_project: Path):
        """Test lazycloud init command."""
        result = run_cli(
            "init",
            "--name",
            E2E_DEPLOY_NAME,
            "--file",
            "docker-compose.yml",
            cwd=temp_project,
        )
        assert result.returncode == 0, f"Init failed: {get_output(result)}"

        lazycloud_file = temp_project / ".lazycloud"
        assert lazycloud_file.exists(), ".lazycloud file not created"

        content = lazycloud_file.read_text()
        assert E2E_DEPLOY_NAME in content

    def test_cli_deploy_and_destroy(self, temp_project: Path):
        """Test full deploy → destroy cycle via CLI with K8s verification."""
        init_result = run_cli(
            "init",
            "--name",
            E2E_DEPLOY_NAME,
            "--file",
            "docker-compose.yml",
            cwd=temp_project,
        )
        assert init_result.returncode == 0, f"Init failed: {get_output(init_result)}"

        deploy_result = run_cli(
            "deploy",
            "--yes",
            cwd=temp_project,
        )
        assert deploy_result.returncode == 0, (
            f"Deploy failed: {get_output(deploy_result)}"
        )

        deployment_id = get_deployment_id(E2E_DEPLOY_NAME)
        if deployment_id:
            success, msg = verify_services(deployment_id, ["web"], timeout=120)
            assert success, f"K8s verification failed: {msg}"

        destroy_result = run_cli(
            "destroy",
            E2E_DEPLOY_NAME,
            "--force",
            cwd=temp_project,
        )
        assert destroy_result.returncode == 0, (
            f"Destroy failed: {get_output(destroy_result)}"
        )

    def test_cli_deploy_invalid_compose(self):
        """Test CLI rejects invalid compose file."""
        invalid_name = "e2e-invalid"
        cleanup_deployment(invalid_name)

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            compose_path = project_dir / "docker-compose.yml"
            compose_path.write_text("""
services:
  INVALID_NAME:
    image: nginx:latest
""")

            init_result = run_cli(
                "init",
                "--name",
                invalid_name,
                "--file",
                "docker-compose.yml",
                cwd=project_dir,
            )
            assert init_result.returncode == 0

            deploy_result = run_cli(
                "deploy",
                "--yes",
                cwd=project_dir,
            )
            assert deploy_result.returncode != 0
            output = get_output(deploy_result)
            assert "invalid" in output or "error" in output or "compliant" in output


class TestCLIMultiServiceDeployment:
    """Test multi-service deployment via CLI."""

    @pytest.fixture
    def multi_service_project(self, multi_service_compose_yaml: str) -> Path:
        """Create temp project with multi-service compose."""
        cleanup_deployment(E2E_MULTI_NAME)

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            compose_path = project_dir / "docker-compose.yml"
            compose_path.write_text(multi_service_compose_yaml)

            yield project_dir

        cleanup_deployment(E2E_MULTI_NAME)

    def test_multi_service_deploy(self, multi_service_project: Path):
        """Test deploying multiple services with K8s verification."""
        init_result = run_cli(
            "init",
            "--name",
            E2E_MULTI_NAME,
            "--file",
            "docker-compose.yml",
            cwd=multi_service_project,
        )
        assert init_result.returncode == 0

        deploy_result = run_cli(
            "deploy",
            "--yes",
            cwd=multi_service_project,
        )
        assert deploy_result.returncode == 0, (
            f"Multi-service deploy failed: {get_output(deploy_result)}"
        )

        deployment_id = get_deployment_id(E2E_MULTI_NAME)
        if deployment_id:
            success, msg = verify_services(deployment_id, ["api", "redis"], timeout=180)
            assert success, f"K8s verification failed: {msg}"

        destroy_result = run_cli(
            "destroy",
            E2E_MULTI_NAME,
            "--force",
            cwd=multi_service_project,
        )
        if destroy_result.returncode != 0:
            print(f"Warning: destroy failed: {get_output(destroy_result)}")


class TestCLISelectiveDeployment:
    """Test selective service deployment."""

    E2E_SELECTIVE_NAME = "e2e-selective"

    @pytest.fixture
    def multi_service_project(self, multi_service_compose_yaml: str) -> Path:
        """Create temp project with multi-service compose."""
        cleanup_deployment(self.E2E_SELECTIVE_NAME)

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            compose_path = project_dir / "docker-compose.yml"
            compose_path.write_text(multi_service_compose_yaml)

            yield project_dir

    def test_selective_service_deploy_requires_existing(
        self, multi_service_project: Path
    ):
        """Test that -s flag requires existing deployment."""
        init_result = run_cli(
            "init",
            "--name",
            self.E2E_SELECTIVE_NAME,
            "--file",
            "docker-compose.yml",
            cwd=multi_service_project,
        )
        assert init_result.returncode == 0

        deploy_result = run_cli(
            "deploy",
            "--yes",
            "-s",
            "api",
            cwd=multi_service_project,
        )
        assert deploy_result.returncode != 0
        output = get_output(deploy_result)
        assert "does not exist" in output or "first" in output
