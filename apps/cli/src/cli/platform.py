import typer
from identity.platform import PlatformNamespaceService
from lazycloud.cli.components.results import emit_result
from provider_clients.settings import PlatformCapacitySettings

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

platform_app = typer.Typer(help="Initialize deployment-owned platform resources offline.")


@platform_app.command("initialize")
def initialize(ctx: typer.Context) -> None:
    binding = PlatformCapacitySettings().aws
    if binding is not None:
        binding.validate_provider()
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Admin).direct()
    )
    try:
        namespace = PlatformNamespaceService(database).initialize()
    finally:
        database.dispose()
    emit_result(
        ctx,
        payload={"status": "ready", "namespace_id": namespace.id},
        title="Platform initialized",
        fields={"Namespace": namespace.id},
    )
