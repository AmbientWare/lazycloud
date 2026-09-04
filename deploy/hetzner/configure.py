"""Prepare a private operator document from a verified Hetzner image manifest."""

from __future__ import annotations

import argparse
import json
import os
import stat
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from uuid import UUID

from compute.providers import ResolvedProviderPolicy
from deploy.hetzner.bake import host_recipe_sha256
from provider_clients.settings import HetznerCapacityBinding, PlatformCapacitySettings
from provider_hetzner.client import HetznerClient, HetznerError
from provider_hetzner.pooled_provider import HetznerNodeImage
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    TypeAdapter,
    ValidationError,
)
from shared.compute_policy import MachinePool
from shared.timestamps import utc_now

_REPOSITORY = Path(__file__).resolve().parents[2]
_OPERATOR_KEY = "LAZYCLOUD_PLATFORM_CAPACITY_HETZNER"


class _BakeMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_image_id: int = Field(gt=0)
    location: str
    bake_id: UUID


class _Build(BaseModel):
    model_config = ConfigDict(extra="ignore")

    builder_type: str
    custom_data: _BakeMetadata


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    builds: list[_Build] = Field(min_length=1, max_length=1)


def _read_private(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 1_048_576
        ):
            raise ValueError("credential inputs must be bounded, owner-only regular files")
        with os.fdopen(descriptor, encoding="utf-8", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _output_directory(path: Path) -> None:
    if path.resolve().is_relative_to(_REPOSITORY):
        raise ValueError("the operator document must remain outside the repository")
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise ValueError("the output directory must belong to the caller with mode 0700")
    if path.exists() or path.is_symlink():
        raise ValueError("output already exists; use a new output file")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--operator-document", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--ref", default="hetzner:platform")
    parser.add_argument("--server-type", action="append", required=True)
    parser.add_argument("--warm-cpu-min", type=int, default=1)
    parser.add_argument("--usd-per-currency-unit", type=Decimal)
    args = parser.parse_args()
    server_types = tuple(dict.fromkeys(args.server_type))

    _output_directory(args.output)
    token = SecretStr(_read_private(args.token_file).strip())
    document = TypeAdapter(dict[str, JsonValue]).validate_json(
        _read_private(args.operator_document)
    )
    existing_value = document.get(_OPERATOR_KEY, "[]")
    if not isinstance(existing_value, str):
        raise ValueError("the existing Hetzner operator value must be a JSON string")
    existing = TypeAdapter(list[dict[str, JsonValue]]).validate_json(existing_value)
    PlatformCapacitySettings(
        hetzner=tuple(HetznerCapacityBinding.model_validate(v) for v in existing)
    )
    manifest = _Manifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    build = manifest.builds[0]
    metadata = build.custom_data
    if build.builder_type != "hcloud" or metadata.recipe_sha256 != host_recipe_sha256(
        metadata.base_image_id
    ):
        raise ValueError("image manifest does not match this host recipe; run Hetzner Node Images")
    client = HetznerClient(token)
    selector = f"lazycloud-bake={metadata.bake_id}"
    if tuple(client.servers(selector)):
        raise ValueError("the image build still has a server; finish its cleanup first")
    images = tuple(client.snapshots(selector))
    if len(images) != 1:
        raise ValueError("the token's project must contain exactly one snapshot from this build")
    image = images[0]
    if (
        image.status != "available"
        or image.architecture != "x86"
        or image.labels.get("lazycloud-release") != metadata.recipe_sha256[:63]
    ):
        raise ValueError("the prepared snapshot is unavailable or does not match its recipe")
    policy = ResolvedProviderPolicy(
        workspace_id=str(args.workspace_id),
        pool=MachinePool("lazycloud"),
        platform_fleet=True,
        default_region=metadata.location,
        allowed_regions=(metadata.location,),
        allowed_instance_types=server_types,
        warm_cpu_min=args.warm_cpu_min,
    )
    shapes = {item.name: item for item in client.server_types()}
    prices: dict[str, Decimal] = {}
    for name in server_types:
        shape = shapes.get(name)
        if (
            shape is None
            or shape.architecture != "x86"
            or shape.cpu_type != "dedicated"
            or shape.disk < policy.root_volume_gib
        ):
            raise ValueError("select dedicated x86 servers with enough disk for the node policy")
        location = next((v for v in shape.locations if v.name == metadata.location), None)
        if (
            location is not None
            and location.deprecation is not None
            and location.deprecation.unavailable_after <= utc_now()
        ):
            raise ValueError("a selected server type is no longer available in this location")
        price = next((v for v in shape.prices if v.location == metadata.location), None)
        if price is None:
            raise ValueError("a selected server type has no price in this location")
        prices[name] = price.price_hourly.net
    pricing = client.pricing()
    if pricing.currency == "USD":
        if args.usd_per_currency_unit not in {None, Decimal(1)}:
            raise ValueError("USD supplier prices require a conversion of 1")
        conversion = Decimal(1)
    elif args.usd_per_currency_unit is not None and args.usd_per_currency_unit > 0:
        conversion = args.usd_per_currency_unit
    else:
        raise ValueError("non-USD supplier prices require a reviewed --usd-per-currency-unit")
    ipv4_price = next(
        (
            p
            for entry in pricing.primary_ips
            if entry.type == "ipv4"
            for p in entry.prices
            if p.location == metadata.location
        ),
        None,
    )
    if ipv4_price is None:
        raise ValueError("the supplier has no IPv4 price in the selected location")
    ipv4_hourly = int(
        (ipv4_price.price_hourly.net * conversion * 1_000_000).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    binding = HetznerCapacityBinding(
        ref=args.ref,
        api_token=token,
        policy=policy,
        images_by_location={
            metadata.location: HetznerNodeImage(
                image_id=image.id, recipe_sha256=metadata.recipe_sha256
            )
        },
        allowed_server_types=frozenset(server_types),
        usd_per_currency_unit=conversion,
        primary_ipv4_hourly_micros=ipv4_hourly,
    )
    prior = next((v for v in existing if v.get("ref") == binding.ref), None)
    if prior is not None:
        if HetznerCapacityBinding.model_validate(prior) != binding:
            raise ValueError("this binding already differs; review active fleet changes explicitly")
    else:
        value = binding.model_dump(mode="json")
        value["api_token"] = token.get_secret_value()
        existing.append(value)
    PlatformCapacitySettings(
        hetzner=tuple(HetznerCapacityBinding.model_validate(v) for v in existing)
    )
    document[_OPERATOR_KEY] = json.dumps(existing, separators=(",", ":"))
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(f"Prepared private operator document: {args.output}")
    print(f"{binding.ref}: {metadata.location}, warm CPU minimum {policy.warm_cpu_min}")
    for name, price in prices.items():
        hourly = (
            int((price * conversion * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
            + ipv4_hourly
        )
        print(f"{name}: {hourly} USD micros/node-hour")
    print("No provider resources or deployment secrets were changed.")


if __name__ == "__main__":
    try:
        main()
    except ValidationError:
        raise SystemExit(
            "Invalid preparation inputs; check the manifest and capacity settings."
        ) from None
    except HetznerError as exc:
        raise SystemExit(str(exc)) from None
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
