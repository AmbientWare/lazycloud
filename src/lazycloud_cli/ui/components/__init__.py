"""Reusable UI components for LazyCloud CLI.

This module provides consistent UI components for displaying information,
collecting user input, and providing feedback in the terminal.
"""

from lazycloud_cli.ui.components.badges import DeploymentStatusBadge
from lazycloud_cli.ui.components.card import Card, CardGroup
from lazycloud_cli.ui.components.confirmation import (
    ConfirmationDialog,
    DestructiveConfirmationDialog,
    SimpleConfirmationDialog,
    confirm_action,
)
from lazycloud_cli.ui.components.deploy_progress import ServiceStatusDisplay
from lazycloud_cli.ui.components.info_cards import (
    BuildInfoCard,
    DeploymentActionCard,
    ErrorCard,
    InfoCard,
    StatusMessageCard,
    SuccessCard,
    WarningCard,
)
from lazycloud_cli.ui.components.panels import (
    CommandResult,
    ConfirmationPanel,
    DeploymentInfo,
    DeploymentInfoPanel,
)
from lazycloud_cli.ui.components.progress import (
    DeploymentProgress,
    ProgressCard,
    SpinnerProgress,
)
from lazycloud_cli.ui.components.section import Section, SectionGroup
from lazycloud_cli.ui.components.tables import (
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
    "ServiceStatusDisplay",
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
