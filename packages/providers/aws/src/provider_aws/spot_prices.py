from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Protocol

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from shared.aws_connections import AwsAccountNetwork

from .provider_control import (
    AwsProviderControlError,
    AwsProviderControlErrorCode,
    invalid_response_error,
    upstream_error,
)


class AwsSpotPriceClient(Protocol):
    def describe_subnets(self, *, SubnetIds: list[str]) -> Mapping[str, object]: ...

    def describe_spot_price_history(
        self,
        *,
        AvailabilityZoneId: str,
        InstanceTypes: list[str],
        ProductDescriptions: list[str],
        StartTime: datetime,
        MaxResults: int,
        NextToken: str,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class AwsSpotQuote:
    instance_type: str
    availability_zone: str
    compute_hourly_micros: int
    effective_at: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class AwsSpotMarket:
    availability_zones: tuple[str, ...]
    quotes: tuple[AwsSpotQuote, ...]


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Subnet(_Response):
    id: str = Field(alias="SubnetId")
    vpc_id: str = Field(alias="VpcId")
    availability_zone: str = Field(alias="AvailabilityZoneId", min_length=1)


class _Subnets(_Response):
    values: tuple[_Subnet, ...] = Field(alias="Subnets")


class _SpotPrice(_Response):
    instance_type: str = Field(alias="InstanceType")
    availability_zone: str = Field(alias="AvailabilityZoneId")
    product: str = Field(alias="ProductDescription")
    price: Decimal = Field(alias="SpotPrice", gt=0, allow_inf_nan=False)
    timestamp: datetime = Field(alias="Timestamp")


class _SpotPrices(_Response):
    values: tuple[_SpotPrice, ...] = Field(alias="SpotPriceHistory")
    next_token: str = Field(default="", alias="NextToken")


SPOT_QUOTE_REUSE_SECONDS = 60.0
SPOT_QUOTE_CACHE_ENTRIES = 256

type AwsSpotQuoteKey = tuple[str, str, tuple[str, ...]]
"""Account, region and the instance types quoted."""


@dataclass(slots=True)
class AwsSpotQuoteCache:
    """Spot markets by account and region, each reused for a minute.

    Listing offers reads every zone's Spot price history in every region, which
    takes seconds, and a request waiting for a machine lists them. One cache
    lives as long as the process's provider resolver, so providers rebuilt per
    call share it. Entries past their minute are dropped before a new one is
    added, and the oldest go first once it holds its limit.
    """

    reuse_seconds: float = SPOT_QUOTE_REUSE_SECONDS
    max_entries: int = SPOT_QUOTE_CACHE_ENTRIES
    clock: Callable[[], float] = time.monotonic
    _entries: dict[AwsSpotQuoteKey, tuple[float, AwsSpotMarket]] = field(
        default_factory=dict, init=False, repr=False
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def market(self, key: AwsSpotQuoteKey, load: Callable[[], AwsSpotMarket]) -> AwsSpotMarket:
        now = self.clock()
        with self._lock:
            cached = self._entries.get(key)
        if cached is not None and now < cached[0]:
            return cached[1]
        market = load()
        with self._lock:
            for stale in [item for item, entry in self._entries.items() if entry[0] <= now]:
                del self._entries[stale]
            while len(self._entries) >= self.max_entries:
                del self._entries[min(self._entries, key=lambda item: self._entries[item][0])]
            self._entries[key] = (now + self.reuse_seconds, market)
        return market


def load_aws_spot_quotes(
    client: AwsSpotPriceClient,
    *,
    network: AwsAccountNetwork,
    instance_types: tuple[str, ...],
    observed_at: datetime | None = None,
) -> AwsSpotMarket:
    if not instance_types:
        return AwsSpotMarket((), ())
    now = observed_at or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("Spot quote observation time must have a timezone")
    operation = "describe Spot prices"
    latest: dict[tuple[str, str], AwsSpotQuote] = {}
    try:
        subnets = _Subnets.model_validate(
            client.describe_subnets(SubnetIds=list(network.subnet_ids))
        ).values
        if {subnet.id for subnet in subnets} != set(network.subnet_ids) or any(
            subnet.vpc_id != network.vpc_id for subnet in subnets
        ):
            raise invalid_response_error(
                operation, "AWS returned subnets outside the configured network"
            )
        zones = tuple(sorted({subnet.availability_zone for subnet in subnets}))
        for zone in zones:
            token = ""
            seen_tokens: set[str] = set()
            while True:
                page = _SpotPrices.model_validate(
                    client.describe_spot_price_history(
                        AvailabilityZoneId=zone,
                        InstanceTypes=list(instance_types),
                        ProductDescriptions=["Linux/UNIX"],
                        StartTime=now,
                        MaxResults=1000,
                        NextToken=token,
                    )
                )
                for price in page.values:
                    if (
                        price.availability_zone != zone
                        or price.instance_type not in instance_types
                        or price.product != "Linux/UNIX"
                        or price.timestamp.tzinfo is None
                    ):
                        raise invalid_response_error(
                            operation, "AWS returned a Spot price outside the requested scope"
                        )
                    key = (price.instance_type, zone)
                    prior = latest.get(key)
                    if prior is None or price.timestamp > prior.effective_at:
                        latest[key] = AwsSpotQuote(
                            instance_type=price.instance_type,
                            availability_zone=zone,
                            compute_hourly_micros=int(
                                (price.price * 1_000_000).to_integral_value(rounding=ROUND_CEILING)
                            ),
                            effective_at=price.timestamp,
                            observed_at=now,
                        )
                if not page.next_token:
                    break
                if page.next_token in seen_tokens:
                    raise invalid_response_error(
                        operation, "AWS repeated a Spot price pagination token"
                    )
                token = page.next_token
                seen_tokens.add(token)
    except ValidationError as exc:
        raise invalid_response_error(
            operation, "AWS returned malformed Spot price inventory"
        ) from exc
    except ClientError as exc:
        error_code = str(exc.response.get("Error", {}).get("Code", "Unknown"))
        code = (
            AwsProviderControlErrorCode.PermissionDenied
            if error_code in {"UnauthorizedOperation", "AccessDenied", "AccessDeniedException"}
            else AwsProviderControlErrorCode.UpstreamUnavailable
        )
        raise AwsProviderControlError(code, operation=operation, detail=error_code) from exc
    except BotoCoreError as exc:
        raise upstream_error(exc, operation=operation) from exc
    return AwsSpotMarket(zones, tuple(latest[key] for key in sorted(latest)))
