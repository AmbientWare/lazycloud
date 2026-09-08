"""Initialize the single-node Compose Garage and its platform key."""

from __future__ import annotations

import os

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr


class Node(BaseModel):
    id: str


class Cluster(BaseModel):
    layout_version: int = Field(alias="layoutVersion")
    nodes: list[Node]


class Key(BaseModel):
    access_key_id: str = Field(alias="accessKeyId", repr=False)
    secret_access_key: SecretStr = Field(alias="secretAccessKey")
    name: str

    model_config = ConfigDict(hide_input_in_errors=True)


class Bucket(BaseModel):
    id: str


def main() -> None:
    with httpx.Client(
        base_url=os.environ["LAZYCLOUD_GARAGE_ADMIN_ENDPOINT_URL"],
        headers={"Authorization": f"Bearer {os.environ['LAZYCLOUD_GARAGE_ADMIN_TOKEN']}"},
        timeout=30,
    ) as client:

        def request(
            method: str,
            operation: str,
            *,
            body: JsonValue = None,
            params: dict[str, str] | None = None,
        ) -> bytes:
            response = client.request(method, f"/v2/{operation}", json=body, params=params)
            if response.is_error:
                raise RuntimeError(f"Garage {operation} failed with HTTP {response.status_code}")
            return response.content

        cluster = Cluster.model_validate_json(request("GET", "GetClusterStatus"))
        if len(cluster.nodes) != 1:
            raise RuntimeError("Compose initialization requires exactly one Garage node")
        if cluster.layout_version == 0:
            request(
                "POST",
                "UpdateClusterLayout",
                body={
                    "roles": [
                        {
                            "id": cluster.nodes[0].id,
                            "zone": "local",
                            "capacity": 10_000_000_000,
                            "tags": [],
                        }
                    ]
                },
            )
            request("POST", "ApplyClusterLayout", body={"version": 1})
        existing = client.get(
            "/v2/GetKeyInfo",
            params={
                "id": os.environ["LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID"],
                "showSecretKey": "true",
            },
        )
        if existing.status_code == 404:
            key = Key.model_validate_json(
                request(
                    "POST",
                    "ImportKey",
                    body={
                        "accessKeyId": os.environ["LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID"],
                        "secretAccessKey": os.environ["LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY"],
                        "name": "lazycloud-local-platform",
                    },
                )
            )
        elif existing.is_success:
            key = Key.model_validate_json(existing.content)
            if (
                key.name != "lazycloud-local-platform"
                or key.secret_access_key.get_secret_value()
                != os.environ["LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY"]
            ):
                raise RuntimeError("Existing Garage key does not match this local deployment")
        else:
            raise RuntimeError(f"Garage GetKeyInfo failed with HTTP {existing.status_code}")
        request(
            "POST",
            "UpdateKey",
            params={"id": key.access_key_id},
            body={"allow": {"createBucket": True}},
        )
        bucket_name = os.environ["LAZYCLOUD_OBJECT_STORE_BUCKET"]
        response = client.get("/v2/GetBucketInfo", params={"globalAlias": bucket_name})
        if response.status_code == 404:
            bucket = Bucket.model_validate_json(
                request("POST", "CreateBucket", body={"globalAlias": bucket_name})
            )
        elif response.is_success:
            bucket = Bucket.model_validate_json(response.content)
        else:
            raise RuntimeError(f"Garage GetBucketInfo failed with HTTP {response.status_code}")
        request(
            "POST",
            "AllowBucketKey",
            body={
                "bucketId": bucket.id,
                "accessKeyId": key.access_key_id,
                "permissions": {"read": True, "write": True, "owner": True},
            },
        )
    print("Local Garage layout, platform key and application bucket configured.")


if __name__ == "__main__":
    main()
