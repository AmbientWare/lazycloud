from textwrap import dedent

from textual.app import App, ComposeResult
from textual.widgets import Static

from lazycloud_cli.config import config
from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.textual.components import Container
from lazycloud_cli.ui.textual.theme import Icons, lazycloud_theme

from .containers import (
    DeploymentsListView,
    UsageBreakdownTable,
    UsageMainContainer,
    UsageOverviewSection,
    UsageTrendSparkline,
)


class UsageDashboard(App):
    """Main usage dashboard application"""

    CSS_PATH = "../styles.tcss"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("escape", "quit", "Quit"),
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("1", "focus_deployments", "Deployments"),
        ("2", "focus_services", "Services"),
    ]

    def on_mount(self) -> None:
        """Setup the layout once the app is mounted"""
        # Register and activate theme
        self.register_theme(lazycloud_theme)
        self.theme = "lazycloud"

        # Set initial focus to deployments list
        try:
            deployments_list = self.query_one(DeploymentsListView)
            self.set_focus(deployments_list)
        except Exception:
            pass

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
        except Exception:
            pass

        try:
            sparkline = self.query_one(UsageTrendSparkline, UsageTrendSparkline)
            sparkline.refresh_trend()
        except Exception:
            pass

        try:
            deployments_list = self.query_one(DeploymentsListView, DeploymentsListView)
            deployments_list.run_worker(
                deployments_list._fetch_deployments_async(), exclusive=True
            )
        except Exception:
            pass

    def action_focus_deployments(self) -> None:
        """Focus the deployments list"""
        try:
            deployments_list = self.query_one(DeploymentsListView)
            self.set_focus(deployments_list)
        except Exception:
            pass

    def action_focus_services(self) -> None:
        """Focus the services table"""
        try:
            services_table = self.query_one(UsageBreakdownTable)
            self.set_focus(services_table)
        except Exception:
            pass

    def action_quit(self) -> None:
        """Quit the application"""
        self.exit()


def run_usage_dashboard():
    app = UsageDashboard()
    app.run()


if __name__ == "__main__":
    run_usage_dashboard()
