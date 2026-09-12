"""Snapshot infrastructure facts and Helm environment values for one deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Annotated, Literal

import yaml
from provider_clients.provider_definitions import PROVIDER_DEFINITIONS
from provider_clients.release_manifest import AwsReleaseManifest
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
from shared.aws_connections import AwsAccountNetwork, AwsRegion

Name = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class FleetInfrastructure(Contract):
    account_id: Annotated[str, Field(pattern=r"^\d{12}$")]
    role_arn: Annotated[
        str, Field(pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+$")
    ]
    networks: dict[AwsRegion, AwsAccountNetwork] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_identity(self) -> FleetInfrastructure:
        if self.role_arn.split(":", maxsplit=5)[4] != self.account_id:
            raise ValueError("Fleet role must belong to its account")
        return self


class SecretDocuments(Contract):
    platform: Name
    operator: Name


class ServiceAccounts(Contract):
    controlPlane: Name
    scheduler: Name
    secretsReader: Name


class ObjectStoreInfrastructure(Contract):
    endpoint_url: Annotated[str, Field(pattern=r"^https://[a-zA-Z0-9.-]+$")]
    region_name: Name
    force_path_style: bool
    bucket: Name
    workspace_bucket_prefix: Name


class Infrastructure(Contract):
    schema_version: Literal[7]
    deployment: Name
    region: Name
    registry: Name
    repository_prefix: Name
    storage_class: Name
    service_accounts: ServiceAccounts
    object_store: ObjectStoreInfrastructure
    workspace_storage_role_arn: Annotated[
        str, Field(pattern=r"^arn:(aws|aws-us-gov|aws-cn):iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+$")
    ]
    storage_access_bucket: Name
    storage_access_queue_url: Annotated[
        str,
        Field(
            pattern=r"^https://sqs\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?/[0-9]{12}/[A-Za-z0-9_-]+$"
        ),
    ]
    workload_image_repository: Name
    control_principal_arn: Name
    public_origin: Annotated[str, Field(pattern=r"^https://[a-zA-Z0-9.-]+$")]
    redis_host: Name
    hetzner_node_images: dict[Name, HetznerNodeImage] = Field(default_factory=dict)
    fleet: FleetInfrastructure
    secret_documents: SecretDocuments
    secrets_reader_role_arn: Name
    cloudflare_tunnel_id: Name
    database_max_connections: Annotated[int, Field(gt=0)]
    database_pooler_max_connections: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_workspace_storage(self) -> Infrastructure:
        role_parts = self.workspace_storage_role_arn.split(":")
        partition = role_parts[1]
        account = role_parts[4]
        suffix = "amazonaws.com.cn" if partition == "aws-cn" else "amazonaws.com"
        if (
            self.object_store.region_name != self.region
            or self.object_store.endpoint_url != f"https://s3.{self.region}.{suffix}"
            or account != self.fleet.account_id
        ):
            raise ValueError("Workspace storage must use the deployment's AWS account and region")
        return self


_VALUES = TypeAdapter(dict[str, JsonValue])
_STRINGS = TypeAdapter(dict[str, str])
_TAG = TypeAdapter[str](Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")])
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
    previous_pooler = positive_integer.validate_python(
        previous_database.get("poolerMaxConnections"), strict=True
    )
    pooler = max(pooler, previous_pooler)
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
    total = pooler + direct + jobs + reserved
    if total > ceiling:
        raise ValueError(
            f"Database transition requires {total} backend connections, exceeding server "
            f"ceiling {ceiling}: pooler {pooler}, session locks "
            f"{direct}, jobs {jobs}, reserve {reserved}."
        )


def render(
    infrastructure: Infrastructure,
    environment: dict[str, JsonValue],
    *,
    deployment: str,
    tag: str,
    release_manifest_url: str,
    manifest: AwsReleaseManifest,
    generation: int,
) -> dict[str, JsonValue]:
    if infrastructure.deployment != deployment:
        raise ValueError("Infrastructure descriptor belongs to a different deployment")
    _TAG.validate_python(tag)
    _RELEASE.validate_python(release_manifest_url)
    if manifest.source_revision != tag or manifest.manifest_public_url != release_manifest_url:
        raise ValueError("deployment source must match the selected complete release")
    if generation < 1:
        raise ValueError("deployment generation must be positive")
    # Environment overlays cannot replace resource identities.
    owned: dict[str, set[str] | None] = {
        "image": None,
        "release": None,
        "serviceAccounts": None,
        "aws": {"region"},
        "storage": {"className"},
        "database": {"maxConnections", "poolerMaxConnections"},
        "secrets": {"documents", "readerRoleArn"},
        "cloudflared": {"apex", "tunnelId"},
        "fleet": set(FleetInfrastructure.model_fields),
        "deploymentRecord": None,
    }
    for key, fields in owned.items():
        if key not in environment:
            continue
        value = environment[key]
        if fields is None or not isinstance(value, dict) or fields.intersection(value):
            raise ValueError(f"Environment values cannot override infrastructure-owned {key}")
    object_store = infrastructure.object_store
    runtime: dict[str, JsonValue] = {
        "LAZYCLOUD_WORKSPACE_STORAGE_ISSUER": "aws",
        "LAZYCLOUD_AWS_WORKSPACE_STORAGE_ROLE_ARN": infrastructure.workspace_storage_role_arn,
        "LAZYCLOUD_AWS_STORAGE_ACCESS_BUCKET": infrastructure.storage_access_bucket,
        "LAZYCLOUD_AWS_STORAGE_ACCESS_QUEUE_URL": infrastructure.storage_access_queue_url,
        "LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL": object_store.endpoint_url,
        "LAZYCLOUD_OBJECT_STORE_REGION_NAME": object_store.region_name,
        "LAZYCLOUD_OBJECT_STORE_FORCE_PATH_STYLE": str(object_store.force_path_style).lower(),
        "LAZYCLOUD_OBJECT_STORE_BUCKET": object_store.bucket,
        "LAZYCLOUD_OBJECT_STORE_WORKSPACE_BUCKET_PREFIX": object_store.workspace_bucket_prefix,
        "LAZYCLOUD_WORKLOAD_IMAGE_REGISTRY_REPOSITORY": infrastructure.workload_image_repository,
        "LAZYCLOUD_AWS_CONNECTION_CONTROL_PRINCIPAL_ARN": infrastructure.control_principal_arn,
        "LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL": infrastructure.public_origin,
        "LAZYCLOUD_TUNNEL_HOSTNAME": f"tunnels.{infrastructure.public_origin.removeprefix('https://')}",
        "LAZYCLOUD_GITHUB_REDIRECT_URI": f"{infrastructure.public_origin}/auth/github/callback",
        "LAZYCLOUD_REDIS_URL": f"rediss://{infrastructure.redis_host}:6379/0",
        "LAZYCLOUD_RELEASE_MANIFEST_URL": release_manifest_url,
        "LAZYCLOUD_PLATFORM_CAPACITY_HETZNER_IMAGES": TypeAdapter(dict[str, HetznerNodeImage])
        .dump_json(infrastructure.hetzner_node_images)
        .decode(),
    }
    authored_runtime = _STRINGS.validate_python(environment.get("runtime", {}), strict=True)
    if runtime.keys() & authored_runtime.keys():
        raise ValueError("Environment runtime overrides an infrastructure or release value")
    hetzner_policy = PROVIDER_DEFINITIONS["hetzner"].policy
    if hetzner_policy.purchases_enabled:
        missing_locations = (
            set(hetzner_policy.allowed_regions) - infrastructure.hetzner_node_images.keys()
        )
        if missing_locations:
            raise ValueError(
                "Enabled Hetzner purchases require node images for "
                + ", ".join(sorted(missing_locations))
            )
    values = dict(environment)
    values["runtime"] = {**runtime, **authored_runtime}
    if infrastructure.hetzner_node_images:
        token_key = "LAZYCLOUD_PLATFORM_CAPACITY_HETZNER_TOKENS"
        defaults = _VALUES.validate_python(
            yaml.safe_load((Path(__file__).parent / "chart/values.yaml").read_text())
        )
        bindings = _mapping(values, "environment")
        for consumer in ("controlPlane", "scheduler"):
            binding = {
                **_mapping(_mapping(defaults, "environment"), consumer),
                **_mapping(bindings, consumer),
            }
            secret_keys = TypeAdapter(list[str]).validate_python(binding["secretKeys"])
            binding["secretKeys"] = list(dict.fromkeys([*secret_keys, token_key]))
            bindings[consumer] = binding
        values["environment"] = bindings
        secrets = _mapping(values, "secrets")
        secrets["map"] = {**_mapping(secrets, "map"), token_key: "operator"}
        values["secrets"] = secrets
    generated: dict[str, dict[str, JsonValue]] = {
        "image": {
            "registry": infrastructure.registry,
            "repositoryPrefix": infrastructure.repository_prefix,
            "tag": tag,
            "artifacts": dict(manifest.platform_images),
        },
        "release": {
            "active": {
                "generation": generation,
                "manifest_url": release_manifest_url,
                "target": manifest.target.model_dump(mode="json"),
            },
        },
        "serviceAccounts": infrastructure.service_accounts.model_dump(mode="json"),
        "aws": {"region": infrastructure.region},
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
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--previous-values", type=Path)
    parser.add_argument("--previous-chart-values", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        infrastructure = Infrastructure.model_validate_json(args.infrastructure.read_bytes())
        environment = _VALUES.validate_python(yaml.safe_load(args.environment.read_text()))
        manifest = AwsReleaseManifest.model_validate_json(args.manifest.read_bytes())
        release_url = manifest.manifest_public_url
        generation = 1
        previous: dict[str, JsonValue] | None = None
        if args.previous_values is not None:
            previous = _VALUES.validate_python(yaml.safe_load(args.previous_values.read_text()))
            generation = (
                TypeAdapter(int).validate_python(
                    _mapping(_mapping(previous, "release"), "active").get("generation", 0),
                    strict=True,
                )
                + 1
            )
        values = render(
            infrastructure,
            environment,
            deployment=args.deployment,
            tag=manifest.source_revision,
            release_manifest_url=release_url,
            manifest=manifest,
            generation=generation,
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
    print(json.dumps({"output": str(args.output), "tag": manifest.source_revision}))


if __name__ == "__main__":
    main()
