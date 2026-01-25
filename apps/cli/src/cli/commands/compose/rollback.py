import asyncio
from pathlib import Path

import typer
from models.statuses import TaskStatus
from rich.console import Console

from cli.api import APIError, api
from cli.lazycloud_file import LazyCloudFile
from cli.ui.components.info_cards import SubscriptionRequiredCard
from cli.ui.views import RollbackView

console = Console()


def rollback(
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation prompt"),
    revision: int | None = typer.Option(
        None,
        "-r",
        "--revision",
        help="Revision number to rollback to (skips selection)",
    ),
):
    """Rollback a Docker Compose deployment to a previous revision."""
    view = RollbackView(console)

    try:
        lazycloud_file = LazyCloudFile.find_and_load(Path.cwd())
        if not lazycloud_file:
            view.show_error(
                "No .lazycloud file found",
                suggestion="Run 'lazycloud init' to create a deployment configuration",
            )
            raise typer.Exit(1)

        lazycloud_config = lazycloud_file.read()
        deployment_name = lazycloud_config.deployment_name

        deployment = api.deployments.get_deployment(name=deployment_name)
        if not deployment:
            view.show_error(f"Deployment '{deployment_name}' not found")
            raise typer.Exit(1)

        history_response = api.deployments.get_deployment_history(str(deployment.id))
        if not history_response.revisions or len(history_response.revisions) < 2:
            view.show_error(
                "Not enough revision history for rollback (need at least 2 revisions)"
            )
            raise typer.Exit(1)

        latest_revision = history_response.revisions[-1]
        rollbackable_revisions = history_response.revisions[:-1]

        # If revision provided via flag, use it directly
        if revision is not None:
            valid_revisions = [r.revision for r in rollbackable_revisions]
            if revision not in valid_revisions:
                view.show_error(
                    f"Invalid revision {revision}. Valid revisions: {valid_revisions}"
                )
                raise typer.Exit(1)
            selected_revision = revision
            selected_relative = latest_revision.revision - revision

        else:
            selected_revision, selected_relative = view.select_revision(
                latest_revision, rollbackable_revisions
            )

        if not selected_revision:
            view.show_cancelled()
            raise typer.Exit(0)

        if not yes:
            confirmed = view.confirm_rollback(deployment_name, selected_relative)
            if not confirmed:
                view.show_cancelled()
                raise typer.Exit(0)

        try:
            with view.show_rollback_progress(deployment_name, selected_relative):
                final_response = asyncio.run(
                    api.deployments.rollback_deployment(
                        str(deployment.id), selected_revision
                    )
                )

            if final_response.status == TaskStatus.COMPLETED:
                view.show_success(
                    deployment_name,
                    selected_relative,
                )
            else:
                error_msg = final_response.message or "Rollback failed"
                view.show_error(f"Rollback failed: {error_msg}")
                raise typer.Exit(1)

        except APIError as e:
            if e.status_code == 401:
                view.show_error("Authentication failed. Please run 'lazycloud login'")
            elif e.status_code == 402:
                console.print(SubscriptionRequiredCard())
            elif e.status_code and 500 <= e.status_code < 600:
                view.show_error(f"Server error: {e}")
            else:
                view.show_error(f"API error: {e}")
            raise typer.Exit(1)

        except Exception as e:
            view.show_error(f"Rollback failed: {e}")
            raise typer.Exit(1)

    except typer.Exit:
        raise

    except Exception as e:
        view.show_error(f"Unexpected error: {e}")
        raise typer.Exit(1)
