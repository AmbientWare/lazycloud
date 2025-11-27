"""E2E test fixtures and configuration."""

import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

# Default to localhost:8000 (Docker Compose API service)
API_BASE_URL = os.environ.get("E2E_API_URL", "http://localhost:8000")

# Constant workspace name for all E2E tests - avoids hitting workspace limits
E2E_WORKSPACE_NAME = "e2e-test-workspace"


# ============================================================================
# API Health and Status
# ============================================================================


def is_api_running() -> bool:
    """Check if the API is running."""
    try:
        response = httpx.get(f"{API_BASE_URL}/health", timeout=5.0)
        return response.status_code == 200
    except Exception:
        return False


requires_api = pytest.mark.skipif(
    not is_api_running(),
    reason="E2E tests require the API. Run: docker compose up -d",
)


def pytest_collection_modifyitems(config, items):
    """Skip E2E tests if the API isn't running."""
    if is_api_running():
        return

    skip_e2e = pytest.mark.skip(
        reason="E2E tests require the full stack. Run: docker compose up -d"
    )
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)


# ============================================================================
# API Client Fixtures
# ============================================================================


@pytest.fixture
def api_client() -> httpx.Client:
    """Provide an httpx client configured for the API (dev mode - no auth needed)."""
    with httpx.Client(base_url=API_BASE_URL, timeout=30.0) as client:
        yield client


@pytest.fixture
def async_api_client() -> httpx.AsyncClient:
    """Provide an async httpx client configured for the API."""
    return httpx.AsyncClient(base_url=API_BASE_URL, timeout=30.0)


# ============================================================================
# Deployment Helpers
# ============================================================================


def get_deployment_id(deployment_name: str) -> str | None:
    """Get deployment ID by name from the API."""
    try:
        response = httpx.get(
            f"{API_BASE_URL}/v1/deployments",
            params={"name": deployment_name},
            timeout=10.0,
        )
        if response.status_code == 200:
            deployments = response.json()
            if deployments:
                return deployments[0].get("id")

    except Exception:
        pass
    return None


def wait_for_deployment_ready(
    deployment_id: str,
    timeout: int = 120,
    poll_interval: int = 5,
) -> dict | None:
    """Wait for deployment services to be ready. Returns services status or None on timeout."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get(
                f"{API_BASE_URL}/v1/deployments/{deployment_id}/services",
                timeout=10.0,
            )
            if response.status_code == 200:
                services = response.json()
                if services:
                    # Check if all services are Running
                    all_ready = all(
                        svc.get("service", {}).get("status") == "Running"
                        and svc.get("service", {}).get("ready_replicas", 0) > 0
                        for svc in services
                    )
                    if all_ready:
                        return services

        except Exception:
            pass
        time.sleep(poll_interval)
    return None


def verify_services(
    deployment_id: str,
    expected_services: list[str],
    timeout: int = 120,
) -> tuple[bool, str]:
    """Verify expected services are running. Returns (success, message)."""
    services = wait_for_deployment_ready(deployment_id, timeout)

    if services is None:
        return False, "Timeout waiting for services to be ready"

    service_names = {svc.get("service", {}).get("name") for svc in services}
    missing = set(expected_services) - service_names

    if missing:
        return False, f"Missing services: {missing}"

    return True, f"All {len(expected_services)} services running"


# ============================================================================
# CLI Helpers
# ============================================================================


def run_cli(
    *args: str,
    env: dict | None = None,
    cwd: Path | None = None,
    verbose: bool = True,
    timeout: int = 180,
    stream: bool = True,
) -> subprocess.CompletedProcess:
    """Run lazycloud CLI command with real-time output streaming."""
    cmd = ["uv", "run", "lazycloud", *args]
    full_env = {
        **os.environ,
        "LAZYCLOUD_API_BASE_URL": API_BASE_URL,
        **(env or {}),
    }

    if verbose:
        print(f"\n>>> {' '.join(args)}", flush=True)

    if stream:
        # Stream output in real-time
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=full_env,
            cwd=cwd,
        )
        stdout_lines = []
        try:
            for line in process.stdout:
                stdout_lines.append(line)
                if verbose:
                    print(line, end="", flush=True)
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            raise
        return subprocess.CompletedProcess(
            cmd, process.returncode, "".join(stdout_lines), ""
        )

    # Non-streaming fallback
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=full_env,
        cwd=cwd,
        timeout=timeout,
    )

    if verbose:
        if result.stdout.strip():
            print(result.stdout)
        if result.stderr.strip():
            print(f"stderr: {result.stderr}")

    return result


def get_output(result: subprocess.CompletedProcess) -> str:
    """Get combined stdout + stderr for error checking."""
    return f"{result.stdout}\n{result.stderr}".lower()


def cleanup_deployment(name: str):
    """Try to destroy a deployment (ignore errors if it doesn't exist)."""
    run_cli("destroy", name, "--force", verbose=False)


def cleanup_workspace(name: str):
    """Try to destroy a workspace (ignore errors if it doesn't exist)."""
    run_cli("workspace", "destroy", name, "--force", verbose=False)


# ============================================================================
# Compose YAML Fixtures
# ============================================================================


@pytest.fixture
def simple_compose_yaml() -> str:
    """Simple compose YAML for basic deployment tests."""
    return """
services:
  web:
    image: nginx:latest
    ports:
      - "80:80"
"""


@pytest.fixture
def multi_service_compose_yaml() -> str:
    """Multi-service compose YAML."""
    return """
services:
  api:
    image: python:3.11-slim
    ports:
      - "8000:8000"
    command: python -m http.server 8000
  redis:
    image: redis:alpine
    ports:
      - "6379:6379"
"""


@pytest.fixture
def invalid_compose_yaml() -> str:
    """Invalid compose YAML for validation tests."""
    return """
services:
  INVALID_NAME:
    image: nginx:latest
"""


@pytest.fixture
def build_compose_yaml() -> str:
    """Compose YAML with build context for testing image builds."""
    return """
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "8080:8080"
"""


@pytest.fixture
def simple_dockerfile() -> str:
    """Simple Dockerfile for build tests."""
    return """FROM python:3.11-slim
WORKDIR /app
RUN echo 'from http.server import HTTPServer, SimpleHTTPRequestHandler; print("E2E test server starting..."); HTTPServer(("", 8080), SimpleHTTPRequestHandler).serve_forever()' > app.py
CMD ["python", "-u", "app.py"]
"""


@pytest.fixture
def compose_with_env_yaml() -> str:
    """Compose YAML with environment variables."""
    return """
services:
  web:
    image: nginx:latest
    ports:
      - "80:80"
    environment:
      - TEST_VAR=test_value
      - ANOTHER_VAR=${ANOTHER_VAR:-default}
"""


@pytest.fixture
def compose_with_volumes_yaml() -> str:
    """Compose YAML with persistent volumes."""
    return """
services:
  db:
    image: postgres:15
    volumes:
      - pgdata:/var/lib/postgresql/data
    environment:
      - POSTGRES_PASSWORD=test
volumes:
  pgdata:
"""
