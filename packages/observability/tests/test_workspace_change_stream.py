from __future__ import annotations

from datetime import UTC, datetime

from coordination.redis_client import RedisClient, redis_text
from observability.workspace_changes import (
    WorkspaceChangeRepository,
    WorkspaceChangeService,
)
from pydantic import JsonValue, TypeAdapter
from redis.typing import EncodableT, FieldT
from shared.http.workspace_changes import (
    WorkspaceChangeEvent,
    WorkspaceChangeTopic,
    WorkspaceChangeType,
)
from tests.real_redis import RealRedisActors
from tests.redis_fakes import FakeRedis

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def test_workspace_change_repository_bounds_and_deletes_workspace_streams(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    repository = WorkspaceChangeRepository(redis, max_length=25)
    for index in range(225):
        repository.append(
            _change(
                "workspace-a",
                f"resource-{index}",
                event_id=f"event-{index}",
            )
        )
    repository.append(_change("workspace-b", "other", event_id="event-other"))

    retained = _resource_ids(redis, repository, "workspace-a")

    assert 0 < len(retained) <= 125
    assert retained[-1] == "resource-224"
    assert repository.delete_workspace("workspace-a") == 1
    assert _resource_ids(redis, repository, "workspace-a") == []
    assert _resource_ids(redis, repository, "workspace-b") == ["other"]


def test_workspace_change_publication_failure_is_nonfatal() -> None:
    service = WorkspaceChangeService(
        WorkspaceChangeRepository(RedisClient(_FailingStreamRedis(), key_prefix="test"))
    )

    published = service.emit_change(
        workspace_id="workspace-a",
        topic=WorkspaceChangeTopic.Apps,
        change=WorkspaceChangeType.Updated,
        resource_id="app-a",
    )

    assert published is None


def _change(workspace_id: str, resource_id: str, *, event_id: str) -> WorkspaceChangeEvent:
    return WorkspaceChangeEvent(
        event_id=event_id,
        occurred_at=datetime(2026, 7, 13, 12, tzinfo=UTC),
        workspace_id=workspace_id,
        topic=WorkspaceChangeTopic.Apps,
        change=WorkspaceChangeType.Updated,
        resource_id=resource_id,
    )


def _resource_ids(
    redis: RedisClient,
    repository: WorkspaceChangeRepository,
    workspace_id: str,
) -> list[str]:
    key = repository.stream_key(workspace_id)
    return [
        WorkspaceChangeEvent.model_validate_json(redis_text(fields["event"])).resource_id
        for _key, entries in redis.stream_read({key: "0-0"}, count=1_000)
        for _entry_id, fields in entries
    ]


class _FailingStreamRedis(FakeRedis):
    def xadd(
        self,
        name: str,
        fields: dict[FieldT, EncodableT],
        *,
        id: str = "*",
        maxlen: int | None = None,
        approximate: bool = False,
    ) -> str:
        _ = name, fields, id, maxlen, approximate
        raise ConnectionError("Redis unavailable")
