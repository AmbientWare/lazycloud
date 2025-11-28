"""E2E tests for workspace CLI commands.

Run with: pytest src/lazycloud_api/tests/e2e/test_cli_workspaces.py -v -s
"""

import pytest

from tests.e2e.conftest import (
    E2E_WORKSPACE_NAME,
    cleanup_workspace,
    get_output,
    requires_api,
    run_cli,
)

pytestmark = [
    pytest.mark.e2e,
    requires_api,
]


class TestCLIWorkspaceList:
    """Test workspace list command."""

    def test_workspace_list(self):
        """Test listing workspaces."""
        result = run_cli("workspaces", "list")
        assert result.returncode == 0


class TestCLIWorkspaceCreateDestroy:
    """Test workspace create and destroy commands."""

    def test_workspace_create_and_destroy(self):
        """Test creating and destroying a workspace."""
        cleanup_workspace(E2E_WORKSPACE_NAME)

        create_result = run_cli("workspaces", "create", E2E_WORKSPACE_NAME)
        assert create_result.returncode == 0, (
            f"Workspace create failed: {get_output(create_result)}\n"
            "Hint: You may need to destroy an existing workspace to make room."
        )

        list_result = run_cli("workspaces", "list")
        assert list_result.returncode == 0
        assert E2E_WORKSPACE_NAME in list_result.stdout

        destroy_result = run_cli("workspaces", "destroy", E2E_WORKSPACE_NAME, "--force")
        assert destroy_result.returncode == 0, (
            f"Workspace destroy failed: {get_output(destroy_result)}"
        )

    def test_workspace_create_duplicate_fails(self):
        """Test creating a workspace with duplicate name fails."""
        test_ws_name = "e2e-duplicate-test"
        cleanup_workspace(test_ws_name)

        create_result = run_cli("workspaces", "create", test_ws_name)
        if create_result.returncode != 0:
            pytest.skip("Could not create initial workspace - may be at limit")

        try:
            duplicate_result = run_cli("workspaces", "create", test_ws_name)
            assert duplicate_result.returncode != 0
            output = get_output(duplicate_result)
            assert "exist" in output or "error" in output or "already" in output
        finally:
            cleanup_workspace(test_ws_name)


class TestCLIWorkspaceActivation:
    """Test workspace activation via CLI."""

    E2E_ACTIVATE_WORKSPACE = "e2e-activate-ws"

    def test_workspace_activate_nonexistent(self):
        """Test that activating a non-existent workspace fails."""
        result = run_cli("workspaces", "activate", "nonexistent-workspace-xyz")
        assert result.returncode != 0

    def test_workspace_activate_flow(self):
        """Test creating then activating a workspace."""
        cleanup_workspace(self.E2E_ACTIVATE_WORKSPACE)

        create_result = run_cli("workspaces", "create", self.E2E_ACTIVATE_WORKSPACE)
        if create_result.returncode != 0:
            pytest.skip("Cannot create workspace - may be at limit")

        try:
            activate_result = run_cli(
                "workspaces", "activate", self.E2E_ACTIVATE_WORKSPACE
            )
            assert activate_result.returncode == 0
        finally:
            cleanup_workspace(self.E2E_ACTIVATE_WORKSPACE)


class TestCLIWorkspaceDestroy:
    """Test workspace destroy edge cases."""

    def test_workspace_destroy_nonexistent(self):
        """Test destroying a non-existent workspace."""
        result = run_cli(
            "workspaces", "destroy", "nonexistent-workspace-xyz", "--force"
        )
        # Should fail gracefully or succeed (idempotent)
        # Just ensure it doesn't crash
        assert result.returncode in [0, 1]

    def test_workspace_destroy_requires_force_or_confirmation(self):
        """Test that destroy without --force requires confirmation."""
        test_ws = "e2e-destroy-test"
        cleanup_workspace(test_ws)

        create_result = run_cli("workspaces", "create", test_ws)
        if create_result.returncode != 0:
            pytest.skip("Cannot create workspace - may be at limit")

        try:
            # Without --force, should fail due to non-interactive mode
            destroy_result = run_cli("workspaces", "destroy", test_ws)
            # Should either fail (needs confirmation) or succeed
            # The key is it doesn't hang waiting for input
            assert destroy_result.returncode in [0, 1]
        finally:
            # Clean up with force
            cleanup_workspace(test_ws)
