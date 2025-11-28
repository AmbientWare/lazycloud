from textwrap import dedent

from textual.app import App, ComposeResult
from textual.css.query import NoMatches
from textual.widgets import Static

from cli.config import config
from cli.ui.colors import Colors
from cli.ui.textual.components import Container
from cli.ui.textual.theme import Icons, lazycloud_theme

from .containers import (
    ActiveDeploymentsContainer,
    UsageMainContainer,
    UsageOverviewSection,
)


class UsageDashboard(App):
    """Main usage dashboard application"""

    CSS_PATH = [
        "../components/container.tcss",
        "../components/datatable.tcss",
        "../components/listview.tcss",
        "../components/modals/modals.tcss",
        "../styles/utilities.tcss",
        "../styles/dashboard.tcss",
        "../styles/usage.tcss",
    ]
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("escape", "quit", "Quit"),
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
    ]

    def on_mount(self) -> None:
        """Setup the layout once the app is mounted"""
        # Register and activate theme
        self.register_theme(lazycloud_theme)
        self.theme = "lazycloud"

        # Set initial focus to active deployments list
        try:
            active_deployments = self.query_one(ActiveDeploymentsContainer)
            self.set_focus(active_deployments)
        except NoMatches:
            self.log.warning("ActiveDeploymentsContainer not found in layout")
        except Exception as e:
            self.log.error(f"Unexpected error setting focus: {e}")
            raise

    def compose(self) -> ComposeResult:
        """Compose the dashboard layout"""
        if not config.active_workspace_id:
            error_container = Container(id="error-container")
            error_container.border_title = f"{Icons.WARNING} Error"
            with error_container:
                error_text = dedent(f"""
                    [{Colors.Hex.error}]No active workspace.[/{Colors.Hex.error}]
                    
                    [{Colors.Hex.warning}]Use 'lazycloud workspace activate <name>' first.[/{Colors.Hex.warning}]
                """).strip()
                yield Static(error_text, id="error-message")
        else:
            yield UsageMainContainer()

    def action_refresh(self) -> None:
        """Refresh usage data"""
        try:
            overview = self.query_one(UsageOverviewSection, UsageOverviewSection)
            overview.refresh_usage()
        except NoMatches:
            self.log.warning("UsageOverviewSection not found in layout")
        except Exception as e:
            self.log.error(f"Unexpected error refreshing usage data: {e}")
            raise

    def action_quit(self) -> None:
        """Quit the application"""
        self.exit()


def run_usage_dashboard():
    app = UsageDashboard()
    app.run()


if __name__ == "__main__":
    run_usage_dashboard()
