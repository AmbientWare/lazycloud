"""Reusable UI components for LazyCloud CLI.

This module provides consistent UI components for displaying information,
collecting user input, and providing feedback in the terminal.
"""

from cli.ui.components.badges import DeploymentStatusBadge
from cli.ui.components.card import Card, CardGroup
from cli.ui.components.confirmation import (
    ConfirmationDialog,
    DestructiveConfirmationDialog,
    SimpleConfirmationDialog,
    confirm_action,
)
from cli.ui.components.info_cards import (
    BuildInfoCard,
    DeploymentActionCard,
    ErrorCard,
    InfoCard,
    StatusMessageCard,
    SuccessCard,
    WarningCard,
)
from cli.ui.components.panels import (
    CommandResult,
    ConfirmationPanel,
    DeploymentInfo,
    DeploymentInfoPanel,
)
from cli.ui.components.progress import (
    DeploymentProgress,
    ProgressCard,
    SpinnerProgress,
)
from cli.ui.components.section import Section, SectionGroup
from cli.ui.components.tables import (
    create_deployment_list_table,
    format_key_value_list,
)

__all__ = [
    # Enums and data classes
    "DeploymentInfo",
    # Components
    "DeploymentStatusBadge",
    "DeploymentInfoPanel",
    "ConfirmationPanel",
    "CommandResult",
    "Card",
    "CardGroup",
    "Section",
    "SectionGroup",
    "ProgressCard",
    "SpinnerProgress",
    "DeploymentProgress",
    "InfoCard",
    "BuildInfoCard",
    "SuccessCard",
    "WarningCard",
    "ErrorCard",
    "StatusMessageCard",
    "DeploymentActionCard",
    "ConfirmationDialog",
    "DestructiveConfirmationDialog",
    "SimpleConfirmationDialog",
    "confirm_action",
    # Helper functions
    "create_deployment_list_table",
    "format_key_value_list",
]
