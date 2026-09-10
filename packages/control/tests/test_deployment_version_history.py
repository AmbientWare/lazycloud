from api.server.services import ApiServices
from shared.deployment_records import DeploymentSpec


def test_deployment_versions_continue_after_soft_delete(isolated_services: ApiServices) -> None:
    app = isolated_services.apps.create("demo_soft_delete")
    spec = DeploymentSpec(name="demo", handler="module:function", metadata={"app_id": app.id})
    first = isolated_services.deployments.deploy(spec)
    deleted = isolated_services.deployments.delete(first.id)
    second = isolated_services.deployments.deploy(spec)

    assert deleted.version == 1
    assert second.version == 2
    assert isolated_services.deployments.list(app_id=app.id) == [second]
