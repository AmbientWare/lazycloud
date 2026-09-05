"""Snapshot infrastructure facts and Helm environment values for one deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Annotated, Literal

import yaml
from provider_clients.settings import HetznerCapacityBinding
from provider_hetzner import HetznerNodeImage
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

Name = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class FleetInfrastructure(Contract):
    account_id: Annotated[str, Field(pattern=r"^\d{12}$")]
    role_arn: Annotated[
        str, Field(pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+$")
    ]
    vpc_id: Annotated[str, Field(pattern=r"^vpc-[a-f0-9]+$")]
    subnet_ids: Annotated[list[Name], Field(min_length=2, max_length=2)]
    security_group_id: Annotated[str, Field(pattern=r"^sg-[a-f0-9]+$")]

    @model_validator(mode="after")
    def validate_identity(self) -> FleetInfrastructure:
        if self.role_arn.split(":", maxsplit=5)[4] != self.account_id:
            raise ValueError("Fleet role must belong to its account")
        if len(set(self.subnet_ids)) != 2:
            raise ValueError("Fleet subnets must be distinct")
        return self


class SecretDocuments(Contract):
    platform: Name
    operator: Name
    wireguard: Name


class ServiceAccounts(Contract):
    controlPlane: Name
    scheduler: Name
    secretsReader: Name
    wireguardBootstrap: Name


class Infrastructure(Contract):
    schema_version: Literal[2]
    deployment: Name
    region: Name
    registry: Name
    repository_prefix: Name
    storage_class: Name
    service_accounts: ServiceAccounts
    object_bucket: Name
    workspace_bucket_prefix: Name
    workspace_storage_role_arn: Name
    workload_image_repository: Name
    control_principal_arn: Name
    public_origin: Annotated[str, Field(pattern=r"^https://[a-zA-Z0-9.-]+$")]
    redis_host: Name
    hetzner_node_images: Annotated[dict[Name, HetznerNodeImage], Field(min_length=1)]
    fleet: FleetInfrastructure
    secret_documents: SecretDocuments
    secrets_reader_role_arn: Name
    cloudflare_tunnel_id: Name
    database_max_connections: Annotated[int, Field(gt=0)]
    database_pooler_max_connections: Annotated[int, Field(gt=0)]


_VALUES = TypeAdapter(dict[str, JsonValue])
_STRINGS = TypeAdapter(dict[str, str])
_TAG = TypeAdapter[str](Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")])
_DIGEST = TypeAdapter[str](Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")])
_RELEASE = TypeAdapter[str](
    Annotated[str, Field(pattern=r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+/manifest\.json$")]
)


class DatabasePool(Contract):
    poolSize: Annotated[int, Field(gt=0)]
    maxOverflow: Annotated[int, Field(ge=0)]

    @property
    def maximum(self) -> int:
        return self.poolSize + self.maxOverflow


def _mapping(values: dict[str, JsonValue], name: str) -> dict[str, JsonValue]:
    return _VALUES.validate_python(values.get(name, {}))


def check_transition_budget(
    previous_defaults: dict[str, JsonValue],
    previous: dict[str, JsonValue],
    defaults: dict[str, JsonValue],
    proposed: dict[str, JsonValue],
    *,
    ceiling: int,
) -> None:
    positive_integer = TypeAdapter[int](Annotated[int, Field(gt=0)])
    previous_database = {
        **_mapping(previous_defaults, "database"),
        **_mapping(previous, "database"),
    }
    database = {**_mapping(defaults, "database"), **_mapping(proposed, "database")}
    pooler = positive_integer.validate_python(database.get("poolerMaxConnections"), strict=True)
    previous_pooler = previous_database.get("poolerMaxConnections")
    old_direct = 0
    if previous_pooler is None:
        # The recorded pre-pooler deployment still has direct application pools.
        for name, engines in (("controlPlane", 2), ("scheduler", 1), ("wireguard", 1)):
            base = _mapping(previous_defaults, name)
            selected = _mapping(previous, name)
            pool_values = {**_mapping(base, "database"), **_mapping(selected, "database")}
            if not pool_values:
                raise ValueError(f"Existing {name} database pool is undeclared")
            replicas = positive_integer.validate_python(
                selected.get("replicas", base.get("replicas")), strict=True
            )
            old_direct += replicas * engines * DatabasePool.model_validate(pool_values).maximum
    else:
        pooler = max(pooler, positive_integer.validate_python(previous_pooler, strict=True))
    api_replicas = max(
        positive_integer.validate_python(
            _mapping(override, "controlPlane").get(
                "replicas", _mapping(common, "controlPlane").get("replicas")
            ),
            strict=True,
        )
        for common, override in ((previous_defaults, previous), (defaults, proposed))
    )
    direct = 2 * api_replicas
    bootstrap = _mapping(defaults, "bootstrap")
    configured_bootstrap = _mapping(proposed, "bootstrap")
    jobs = DatabasePool.model_validate(
        {**_mapping(bootstrap, "database"), **_mapping(configured_bootstrap, "database")}
    ).maximum
    reserved = max(
        positive_integer.validate_python(values.get("reserved"), strict=True)
        for values in (previous_database, database)
    )
    total = old_direct + pooler + direct + jobs + reserved
    if total > ceiling:
        raise ValueError(
            f"Database transition requires {total} backend connections, exceeding server "
            f"ceiling {ceiling}: old direct {old_direct}, pooler {pooler}, session locks "
            f"{direct}, jobs {jobs}, reserve {reserved}. Stage the owned PgBouncer bound "
            "before migrating direct application connections."
        )


def render(
    infrastructure: Infrastructure,
    environment: dict[str, JsonValue],
    *,
    deployment: str,
    tag: str,
    release_manifest_url: str,
    worker_manifest_url: str,
    host_manifest_url: str,
) -> dict[str, JsonValue]:
    if infrastructure.deployment != deployment:
        raise ValueError("Infrastructure descriptor belongs to a different deployment")
    _TAG.validate_python(tag)
    for url in (release_manifest_url, worker_manifest_url, host_manifest_url):
        _RELEASE.validate_python(url)
    # Environment overlays cannot replace resource identities.
    owned: dict[str, set[str] | None] = {
        "image": None,
        "serviceAccounts": None,
        "storage": {"className"},
        "database": {"maxConnections", "poolerMaxConnections"},
        "secrets": {"documents", "readerRoleArn"},
        "cloudflared": {"apex", "tunnelId"},
        "wireguard": {"secretId"},
        "fleet": set(FleetInfrastructure.model_fields),
        "deploymentRecord": None,
    }
    for key, fields in owned.items():
        if key not in environment:
            continue
        value = environment[key]
        if fields is None or not isinstance(value, dict) or fields.intersection(value):
            raise ValueError(f"Environment values cannot override infrastructure-owned {key}")
    runtime: dict[str, JsonValue] = {
        "LAZYCLOUD_OBJECT_STORE_BUCKET": infrastructure.object_bucket,
        "LAZYCLOUD_OBJECT_STORE_WORKSPACE_BUCKET_PREFIX": infrastructure.workspace_bucket_prefix,
        "LAZYCLOUD_OBJECT_STORE_REGION_NAME": infrastructure.region,
        "LAZYCLOUD_WORKSPACE_STORAGE_ISSUER": "aws",
        "LAZYCLOUD_WORKSPACE_STORAGE_ROLE_ARN": infrastructure.workspace_storage_role_arn,
        "LAZYCLOUD_WORKSPACE_STORAGE_REGION_NAME": infrastructure.region,
        "LAZYCLOUD_WORKLOAD_IMAGE_REGISTRY_REPOSITORY": infrastructure.workload_image_repository,
        "LAZYCLOUD_AWS_CONNECTION_CONTROL_PRINCIPAL_ARN": infrastructure.control_principal_arn,
        "LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL": infrastructure.public_origin,
        "LAZYCLOUD_GITHUB_REDIRECT_URI": f"{infrastructure.public_origin}/auth/github/callback",
        "LAZYCLOUD_REDIS_URL": f"rediss://{infrastructure.redis_host}:6379/0",
        "LAZYCLOUD_RELEASE_MANIFEST_URL": release_manifest_url,
        "LAZYCLOUD_RELEASE_WORKER_MANIFEST_URL": worker_manifest_url,
        "LAZYCLOUD_RELEASE_HOST_MANIFEST_URL": host_manifest_url,
    }
    authored_runtime = _STRINGS.validate_python(environment.get("runtime", {}), strict=True)
    if runtime.keys() & authored_runtime.keys():
        raise ValueError("Environment runtime overrides an infrastructure or release value")
    rates = TypeAdapter(dict[str, Annotated[int, Field(gt=0)]]).validate_json(
        authored_runtime.get("LAZYCLOUD_AWS_CAPACITY_INSTANCE_HOURLY_MICROS", "")
    )
    if not rates:
        raise ValueError("Managed fleet requires nonempty instance prices")
    bindings = TypeAdapter(list[dict[str, JsonValue]]).validate_json(
        authored_runtime.get("LAZYCLOUD_PLATFORM_CAPACITY_HETZNER", "")
    )
    if not bindings:
        raise ValueError("Platform capacity requires a configured Hetzner binding")
    resolved_bindings: list[HetznerCapacityBinding] = []
    for binding in bindings:
        if "images_by_location" in binding:
            raise ValueError("Environment capacity overrides infrastructure-owned images")
        resolved_bindings.append(
            HetznerCapacityBinding.model_validate(
                {**binding, "images_by_location": infrastructure.hetzner_node_images}
            )
        )
    authored_runtime["LAZYCLOUD_PLATFORM_CAPACITY_HETZNER"] = (
        TypeAdapter(list[HetznerCapacityBinding]).dump_json(resolved_bindings).decode()
    )
    values = dict(environment)
    values["runtime"] = {**runtime, **authored_runtime}
    generated: dict[str, dict[str, JsonValue]] = {
        "image": {
            "registry": infrastructure.registry,
            "repositoryPrefix": infrastructure.repository_prefix,
            "tag": tag,
        },
        "serviceAccounts": infrastructure.service_accounts.model_dump(mode="json"),
        "storage": {"className": infrastructure.storage_class},
        "database": {
            "maxConnections": infrastructure.database_max_connections,
            "poolerMaxConnections": infrastructure.database_pooler_max_connections,
        },
        "secrets": {
            "documents": infrastructure.secret_documents.model_dump(mode="json"),
            "readerRoleArn": infrastructure.secrets_reader_role_arn,
        },
        "cloudflared": {
            "apex": infrastructure.public_origin.removeprefix("https://"),
            "tunnelId": infrastructure.cloudflare_tunnel_id,
        },
        "wireguard": {"secretId": infrastructure.secret_documents.wireguard},
        "fleet": infrastructure.fleet.model_dump(mode="json"),
    }
    for key, facts in generated.items():
        configured = values.get(key, {})
        if not isinstance(configured, dict):
            raise ValueError(f"{key} must be a mapping")
        values[key] = {**configured, **facts}
    values["deploymentRecord"] = {
        "infrastructureSha256": hashlib.sha256(
            infrastructure.model_dump_json().encode()
        ).hexdigest(),
    }
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--infrastructure", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--tag", required=True)
    artifact = parser.add_mutually_exclusive_group(required=True)
    artifact.add_argument("--preflight", action="store_true")
    artifact.add_argument("--network-digest")
    parser.add_argument("--release-manifest-url", default="")
    parser.add_argument("--worker-manifest-url", default="")
    parser.add_argument("--host-manifest-url", default="")
    parser.add_argument("--previous-values", type=Path)
    parser.add_argument("--previous-chart-values", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        infrastructure = Infrastructure.model_validate_json(args.infrastructure.read_bytes())
        environment = _VALUES.validate_python(yaml.safe_load(args.environment.read_text()))
        release_url = args.release_manifest_url
        worker_url = args.worker_manifest_url or release_url
        host_url = args.host_manifest_url
        previous: dict[str, JsonValue] | None = None
        if args.previous_values is not None:
            previous = _VALUES.validate_python(yaml.safe_load(args.previous_values.read_text()))
            previous_runtime = _STRINGS.validate_python(previous.get("runtime", {}), strict=True)
            release_url = release_url or previous_runtime.get("LAZYCLOUD_RELEASE_MANIFEST_URL", "")
            worker_url = worker_url or previous_runtime.get(
                "LAZYCLOUD_RELEASE_WORKER_MANIFEST_URL", ""
            )
            host_url = host_url or previous_runtime.get("LAZYCLOUD_RELEASE_HOST_MANIFEST_URL", "")
        values = render(
            infrastructure,
            environment,
            deployment=args.deployment,
            tag=args.tag,
            release_manifest_url=release_url,
            worker_manifest_url=worker_url,
            host_manifest_url=host_url,
        )
        if previous is not None:
            if args.previous_chart_values is None:
                raise ValueError(
                    "Previous deployment requires its chart defaults for pool budgeting"
                )
            check_transition_budget(
                _VALUES.validate_python(yaml.safe_load(args.previous_chart_values.read_text())),
                previous,
                _VALUES.validate_python(
                    yaml.safe_load((Path(__file__).parent / "chart/values.yaml").read_text())
                ),
                values,
                ceiling=infrastructure.database_max_connections,
            )
        if not args.preflight:
            image = _mapping(values, "image")
            image["networkDigest"] = _DIGEST.validate_python(args.network_digest)
            values["image"] = image
        args.output.write_text(yaml.safe_dump(values, sort_keys=True))
    except ValidationError as error:
        locations = [".".join(map(str, item["loc"])) for item in error.errors(include_input=False)]
        print(f"Invalid deployment configuration at: {', '.join(locations)}", file=sys.stderr)
        raise SystemExit(1) from None
    except yaml.YAMLError:
        print("Invalid environment YAML", file=sys.stderr)
        raise SystemExit(1) from None
    except (ValueError, OSError) as error:
        print(f"Invalid deployment configuration: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps({"output": str(args.output), "tag": args.tag}))


if __name__ == "__main__":
    main()
