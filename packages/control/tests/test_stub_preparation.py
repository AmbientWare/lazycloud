from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.apps import StubRepository
from database.repositories.orchestration import ContainerRepository
from database.repositories.storage import ObjectRepository
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployments import StubKind
from shared.errors import ConflictError
from shared.objects import ObjectRecord
from shared.workload_config import StubConfig, StubImageConfig, StubRuntimeConfig


def test_preparing_function_keeps_same_named_endpoint_container_on_its_revision(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    endpoint = control.create_stub(
        "print_hello",
        kind=StubKind.Endpoint,
        handler="main:print_hello",
        metadata={"app": "quickstart"},
    )
    with isolated_services.context.database.session() as session:
        container = ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="print_hello",
                image="python:3.12-slim",
                command=["python", "-m", "runner.serve"],
                workspace_id=endpoint.workspace_id,
                stub_id=endpoint.id,
                status=ContainerStatus.Running,
            )
        )
    function = control.create_stub(
        "print_hello",
        kind=StubKind.Function,
        handler="main:print_hello",
        metadata={"app": "quickstart"},
    )

    assert function.id != endpoint.id
    assert function.name == endpoint.name == "print_hello"
    assert control.get_stub(endpoint.id).kind is StubKind.Endpoint
    with isolated_services.context.database.session() as session:
        persisted = ContainerRepository(session).get_across_workspaces(container.id)
    assert persisted is not None
    assert persisted.stub_id == endpoint.id
    assert persisted.status is ContainerStatus.Running
    assert not control.discard_registration_source_stub(endpoint.id)
    with pytest.raises(ConflictError):
        control.get_stub("print_hello", workspace=endpoint.workspace_id)
    with pytest.raises(ConflictError):
        isolated_services.apps.create(
            "quickstart", stub_id="print_hello", workspace=endpoint.workspace_id
        )


def test_source_and_runtime_changes_prepare_distinct_reusable_revisions(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = control.get_workspace()
    sources = [
        ObjectRecord(
            id=str(uuid4()),
            bucket="objects",
            key=f"source-{index}",
            path=f"/objects/source-{index}",
            size=1,
            sha256=str(index),
        )
        for index in range(2)
    ]
    with isolated_services.context.database.session() as session:
        for source in sources:
            ObjectRepository(session).upsert(source, workspace_id=workspace.id)
    original_config = StubConfig(
        object_id=sources[0].id,
        image=StubImageConfig(image_id="image-v1"),
        runtime=StubRuntimeConfig(cpu=1, memory="256Mi"),
    )
    original = control.create_stub("hello", handler="main:hello", config=original_config)
    source_config = original_config.model_copy(update={"object_id": sources[1].id}, deep=True)
    source_changed = control.create_stub("hello", handler="main:hello", config=source_config)
    runtime_config = source_config.model_copy(
        update={"runtime": StubRuntimeConfig(cpu=2, memory="512Mi")}, deep=True
    )
    runtime_changed = control.create_stub("hello", handler="main:hello", config=runtime_config)
    image_config = runtime_config.model_copy(
        update={"image": StubImageConfig(image_id="image-v2")}, deep=True
    )
    image_changed = control.create_stub("hello", handler="main:hello", config=image_config)

    assert len({original.id, source_changed.id, runtime_changed.id, image_changed.id}) == 4
    assert control.get_stub(original.id).config == original_config
    assert (
        control.create_stub("hello", handler="main:hello", config=original_config).id == original.id
    )
    assert (
        control.create_stub("hello", handler="main:hello", config=image_config).id
        == image_changed.id
    )


def test_concurrent_prepares_reuse_one_revision_but_keep_app_scope(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    start = Barrier(4)

    def prepare() -> str:
        start.wait(timeout=5)
        return control.create_stub("hello", handler="main:hello", metadata={"app": "first"}).id

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(prepare) for _ in range(4)]
        ids = [future.result(timeout=10) for future in futures]
    assert len(set(ids)) == 1
    other_app = control.create_stub("hello", handler="main:hello", metadata={"app": "second"})
    assert other_app.id != ids[0]


def test_preparation_does_not_reuse_unverified_or_explicitly_patched_definitions(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    unverified = control.create_stub("hello", handler="main:hello", reuse_existing=False)
    prepared = control.create_stub("hello", handler="main:hello")
    assert prepared.id != unverified.id

    control.update_stub_config(prepared.id, fields={"runtime.cpu": 2})
    replacement = control.create_stub("hello", handler="main:hello")
    assert replacement.id not in {prepared.id, unverified.id}
    assert control.get_stub(prepared.id).config.runtime.cpu == 2
    assert replacement.config.runtime.cpu is None

    with isolated_services.context.database.session() as session:
        repository = StubRepository(session)
        changed = repository.get(replacement.id, workspace_id=replacement.workspace_id)
        assert changed is not None
        changed.handler = "main:changed"
        repository.upsert(changed)
    after_old_writer = control.create_stub("hello", handler="main:hello")
    assert after_old_writer.id != replacement.id
    assert control.get_stub(replacement.id).handler == "main:changed"
