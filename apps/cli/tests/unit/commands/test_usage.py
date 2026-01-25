"""Unit tests for usage command

Note: The usage command launches a Textual TUI app which is difficult to
unit test meaningfully. These tests focus on ensuring the command properly
invokes the TUI. Full dashboard UI behavior is better suited for E2E tests.
"""

import pytest
from unittest.mock import MagicMock

from cli.commands.usage import usage


@pytest.mark.unit
class TestUsage:
    """Tests for usage command"""

    def test_usage_launches_dashboard(self, mock_config_dir, mocker):
        """Test usage command launches the usage dashboard"""
        # Mock the usage UI
        mock_run_usage = mocker.patch("cli.commands.usage.run_usage")

        # Create a mock context with no subcommand
        mock_ctx = MagicMock()
        mock_ctx.invoked_subcommand = None

        usage(mock_ctx)

        mock_run_usage.assert_called_once()
