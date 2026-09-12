from __future__ import annotations

from api.server.services import ApiServices
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.http.collections import (
    MapGetResponse,
    SimpleQueueListResponse,
    SimpleQueuePopResponse,
    decode_bytes,
    encode_bytes,
)
from shared.identity import AuthScope, WorkspaceRecord


def test_resource_routes_canonicalize_workspace_name_and_id(
    api_runtime: tuple[ApiServices, TestClient],
    api_workspace: WorkspaceRecord,
) -> None:
    services, client = api_runtime
    workspace = services.control_plane_service.ensure_workspace_storage(api_workspace.id)
    headers = _headers(services, workspace.id)

    map_set = client.post(
        f"/api/v1/maps/demo-map/set?workspace={workspace.name}",
        headers=headers,
        json={
            "key": "result",
            "value_base64": encode_bytes(b"map-value"),
            "ttl_seconds": 600,
        },
    )
    assert map_set.status_code == 200
    assert map_set.json() == {}

    map_list = client.get(
        f"/api/v1/maps?workspace={workspace.id}",
        headers=headers,
    )
    assert map_list.status_code == 200
    map_page = map_list.json()
    assert 0 < map_page["maps"][0].pop("nearest_expiry_seconds") <= 600
    assert map_page == {
        "maps": [
            {
                "name": "demo-map",
                "count": 1,
                "size_bytes": len(b"map-value"),
                "expiring_keys": 1,
            }
        ]
    }

    map_get = client.get(
        f"/api/v1/maps/demo-map/get?workspace={workspace.id}&key=result",
        headers=headers,
    )
    assert map_get.status_code == 200
    map_value = MapGetResponse.model_validate_json(map_get.content)
    assert decode_bytes(map_value.value_base64) == b"map-value"

    queue_put = client.post(
        f"/api/v1/simplequeues/demo-queue/put?workspace={workspace.name}",
        headers=headers,
        json={"value_base64": encode_bytes(b"queue-value")},
    )
    assert queue_put.status_code == 200
    assert queue_put.json() == {}

    queue_list = client.get(
        f"/api/v1/simplequeues?workspace={workspace.id}",
        headers=headers,
    )
    assert queue_list.status_code == 200
    queue_info = SimpleQueueListResponse.model_validate_json(queue_list.content).queues[0]
    assert queue_info.name == "demo-queue"
    assert queue_info.size == 1
    assert queue_info.oldest_message_age_seconds is not None
    assert queue_info.oldest_message_age_seconds >= 0
    assert queue_info.put_rate_per_minute == 1

    queue_pop = client.post(
        f"/api/v1/simplequeues/demo-queue/pop?workspace={workspace.id}",
        headers=headers,
    )
    assert queue_pop.status_code == 200
    queue_value = SimpleQueuePopResponse.model_validate_json(queue_pop.content)
    assert decode_bytes(queue_value.value_base64) == b"queue-value"

    signal_set = client.post(
        f"/api/v1/signals/demo-signal/set?workspace={workspace.name}",
        headers=headers,
        json={},
    )
    assert signal_set.status_code == 200
    assert signal_set.json() == {}

    signal_monitor = client.get(
        f"/api/v1/signals/demo-signal/monitor?workspace={workspace.id}",
        headers=headers,
    )
    assert signal_monitor.status_code == 200
    assert signal_monitor.json() == {"set": True}

    volume_create = client.post(
        f"/api/v1/volumes?workspace={workspace.name}",
        headers=headers,
        json={"name": "demo-volume"},
    )
    assert volume_create.status_code == 201
    assert volume_create.json()["volume"]["name"] == "demo-volume"

    volume_copy = client.post(
        f"/api/v1/volumes/copy-path?workspace={workspace.id}",
        headers=headers,
        json={
            "path": "demo-volume/result.txt",
            "value_base64": encode_bytes(b"volume-value"),
        },
    )
    assert volume_copy.status_code == 200

    volume_stat = client.get(
        f"/api/v1/volumes/demo-volume%2Fresult.txt/stat?workspace={workspace.id}",
        headers=headers,
    )
    assert volume_stat.status_code == 200
    assert volume_stat.json()["path_info"]["size"] == len(b"volume-value")


def _headers(services: ApiServices, workspace_id: str) -> dict[str, str]:
    token, _ = AuthService(services.context).create_token(
        "resource-route-test",
        workspace_id=workspace_id,
        scopes=[AuthScope.Read.value, AuthScope.Write.value],
    )
    return {"Authorization": f"Bearer {token}"}
