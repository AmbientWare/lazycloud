from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

import httpx
from pydantic import Field
from shared.billing_plans import BillingPlanId
from shared.billing_quotes import NANOS_PER_USD, BilledDimension
from shared.enums import StringEnum
from shared.errors import ConflictError, InvalidInputError
from shared.payments import BILLING_CURRENCY, METER_EVENT_NAMES

from provider_stripe.api import FormFields, StripeObject, read

NANODOLLAR_UNIT_LABEL = "nanodollars"
NANODOLLAR_UNIT_AMOUNT = "0.0000001"
"""One cent per ten million units, so one unit is one nanodollar.

Cost leaves the ledger in nanodollars and is metered in nanodollars, so the
provider's arithmetic is a multiplication by one and an invoice can be compared
against the ledger as integers.
"""

NANOS_PER_CENT = NANOS_PER_USD // 100


def cents(amount_nanos: int) -> int:
    """Stripe's unit for a fixed amount, from the nanodollars this platform holds.

    Only the flat figures — a plan price, an allowance — cross in this unit;
    usage crosses in nanodollars through the metered prices above. A figure that
    is not a whole number of cents is refused rather than rounded: rounding here
    would publish a price nobody chose, and it would do it to the one number a
    customer checks against their statement.
    """

    if amount_nanos < 0:
        raise InvalidInputError("a published amount cannot be negative")
    whole, remainder = divmod(amount_nanos, NANOS_PER_CENT)
    if remainder:
        raise InvalidInputError(f"{amount_nanos} nanodollars is not a whole number of cents")
    return whole


@dataclass(frozen=True, slots=True)
class PlanLine:
    """One plan as the customer meets it on an invoice.

    A product per plan rather than one product priced twice, because the product
    name is what the line is called and a customer on the free plan reading
    "LazyCloud Team" at $0.00 has been told something untrue.
    """

    plan: BillingPlanId
    product_id: str
    product_name: str
    price_lookup_key: str


PLAN_LINES: tuple[PlanLine, ...] = (
    PlanLine(
        plan=BillingPlanId.Free,
        product_id="lazycloud_plan_free",
        product_name="LazyCloud Free",
        price_lookup_key="lazycloud_plan_free_monthly_usd",
    ),
    PlanLine(
        plan=BillingPlanId.Team,
        product_id="lazycloud_plan_team",
        product_name="LazyCloud Team",
        price_lookup_key="lazycloud_plan_team_monthly_usd",
    ),
)

_PLAN_LINES_BY_PLAN: Mapping[BillingPlanId, PlanLine] = {line.plan: line for line in PLAN_LINES}

if _PLAN_LINES_BY_PLAN.keys() != set(BillingPlanId):
    raise RuntimeError(
        "every plan an account can be put on must have a published product and price"
    )


_PLANS_BY_PRICE_LOOKUP_KEY: Mapping[str, BillingPlanId] = {
    line.price_lookup_key: line.plan for line in PLAN_LINES
}


def plan_line(plan: BillingPlanId) -> PlanLine:
    """The product and price one plan is sold through.

    Total over the enum, held so by the check above: a plan a caller can ask for
    and no price to subscribe them to would be a subscribe that fails at the
    provider with nothing here able to say why.
    """

    return _PLAN_LINES_BY_PLAN[plan]


def plan_for_price_lookup_key(lookup_key: str) -> BillingPlanId | None:
    """The way back from `plan_line`, kept beside it so the correspondence
    between a plan and the price it is sold through is stated once. `None` for a
    price this catalog did not publish."""

    return _PLANS_BY_PRICE_LOOKUP_KEY.get(lookup_key)


@dataclass(frozen=True, slots=True)
class UsageLine:
    """One dimension as the customer meets it on an invoice.

    A product per dimension rather than one product with three prices, because
    the product name is what a line is called: sharing one would print the plan's
    name three times and hide the breakdown the three meters exist to produce.
    """

    dimension: BilledDimension
    product_id: str
    product_name: str
    price_lookup_key: str

    @property
    def meter_event_name(self) -> str:
        return METER_EVENT_NAMES[self.dimension]


USAGE_LINES: tuple[UsageLine, ...] = (
    UsageLine(
        dimension=BilledDimension.ComputeRuntime,
        product_id="lazycloud_usage_compute",
        product_name="LazyCloud Compute",
        price_lookup_key="lazycloud_meter_compute_usd",
    ),
    UsageLine(
        dimension=BilledDimension.NetworkEgress,
        product_id="lazycloud_usage_egress",
        product_name="LazyCloud Network Egress",
        price_lookup_key="lazycloud_meter_egress_usd",
    ),
    UsageLine(
        dimension=BilledDimension.VolumeStorage,
        product_id="lazycloud_usage_volume_storage",
        product_name="LazyCloud Volume Storage",
        price_lookup_key="lazycloud_meter_volume_storage_usd",
    ),
)

METERED_PRICE_LOOKUP_KEYS: tuple[str, ...] = tuple(line.price_lookup_key for line in USAGE_LINES)


def subscription_price_lookup_keys(plan: BillingPlanId) -> tuple[str, ...]:
    """Every price one subscription carries: the plan's, then one per dimension.

    All three usage prices ride on every subscription, including the two priced
    at zero and including the free plan's. A customer seeing a $0.00 egress line
    is seeing that it is measured and free, where a customer seeing no line at
    all cannot tell that from not being measured — and on the free plan it is
    load-bearing beyond presentation: without the metered prices, usage past what
    the plan includes reaches no invoice at all.
    """

    return (plan_line(plan).price_lookup_key, *METERED_PRICE_LOOKUP_KEYS)


class CatalogObjectKind(StringEnum):
    Meter = "meter"
    Product = "product"
    Price = "price"


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One object the catalog is made of, and whether the account holds it."""

    kind: CatalogObjectKind
    name: str
    """The deterministic name this platform addresses the object by: a product
    id, a price lookup key, or a meter event name. Never the provider's own
    generated identifier, which nothing here stores."""

    summary: str
    provider_id: str = ""
    published_summary: str = ""
    """What the account holds instead, when that differs from `summary`. Empty
    when the two agree, which is every object the account holds as this
    repository describes it."""
    """Empty where the object does not exist yet."""

    @property
    def present(self) -> bool:
        return bool(self.provider_id)


@dataclass(frozen=True, slots=True)
class PublishedCatalog:
    account_id: str
    entries: tuple[CatalogEntry, ...]

    @property
    def missing(self) -> tuple[CatalogEntry, ...]:
        return tuple(entry for entry in self.entries if not entry.present)

    @property
    def stale(self) -> tuple[CatalogEntry, ...]:
        """Objects the account holds at a figure this repository no longer publishes."""

        return tuple(entry for entry in self.entries if entry.published_summary)


class _Account(StripeObject):
    id: str


class _Product(StripeObject):
    id: str


class _ProductList(StripeObject):
    data: list[_Product] = Field(default_factory=list)


class _Recurring(StripeObject):
    meter: str | None = None


class _Price(StripeObject):
    id: str
    lookup_key: str | None = None
    unit_amount: int | None = None
    unit_amount_decimal: str | None = None
    recurring: _Recurring | None = None


class _PriceList(StripeObject):
    data: list[_Price] = Field(default_factory=list)


class _Meter(StripeObject):
    id: str
    event_name: str


class _MeterList(StripeObject):
    data: list[_Meter] = Field(default_factory=list)
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class StripeCatalog:
    """The plans, the meters and the prices, published by deterministic name.

    Read before written and never deleted. Every object is addressed by a name
    this repository chose — a product id, a price lookup key, a meter event name
    — so nothing has to store what the provider generated and no lookup table has
    to be kept in step with the account.
    """

    client: httpx.Client

    def account_id(self) -> str:
        """Which account the credential in hand belongs to.

        Read so the operator can be held to naming it. A catalog published into
        the wrong account is objects a live account carries until somebody works
        out whether they matter.
        """

        return read(_Account, self.client, "GET", "/account").id

    def published(self, *, plan_prices: Mapping[BillingPlanId, int]) -> PublishedCatalog:
        """What the account holds now, and what publishing would add."""

        plan_price_cents = {plan: cents(nanos) for plan, nanos in plan_prices.items()}
        meters = self._meters()
        products = self._products()
        prices = self._prices()
        entries: list[CatalogEntry] = []
        for line in USAGE_LINES:
            entries.append(
                CatalogEntry(
                    kind=CatalogObjectKind.Meter,
                    name=line.meter_event_name,
                    summary=f"sum of {line.dimension.value} nanodollars, keyed by customer id",
                    provider_id=meters.get(line.meter_event_name, ""),
                )
            )
        entries.extend(
            CatalogEntry(
                kind=CatalogObjectKind.Product,
                name=line.product_id,
                summary=line.product_name,
                provider_id=line.product_id if line.product_id in products else "",
            )
            for line in PLAN_LINES
        )
        entries.extend(
            CatalogEntry(
                kind=CatalogObjectKind.Product,
                name=line.product_id,
                summary=f"{line.product_name}, billed in {NANODOLLAR_UNIT_LABEL}",
                provider_id=line.product_id if line.product_id in products else "",
            )
            for line in USAGE_LINES
        )
        entries.extend(
            CatalogEntry(
                kind=CatalogObjectKind.Price,
                name=line.price_lookup_key,
                summary=f"{plan_price_cents[line.plan]} cents monthly, licensed",
                provider_id=self._price_id(prices, line.price_lookup_key),
                published_summary=self._plan_price_disagreement(
                    prices,
                    line.price_lookup_key,
                    plan_price_cents[line.plan],
                ),
            )
            for line in PLAN_LINES
            if line.plan in plan_price_cents
        )
        entries.extend(
            CatalogEntry(
                kind=CatalogObjectKind.Price,
                name=line.price_lookup_key,
                summary=(
                    f"{NANODOLLAR_UNIT_AMOUNT} cents per unit, metered monthly "
                    f"on {line.meter_event_name}"
                ),
                provider_id=self._price_id(prices, line.price_lookup_key),
            )
            for line in USAGE_LINES
        )
        return PublishedCatalog(account_id=self.account_id(), entries=tuple(entries))

    def publish(self, *, plan_prices: Mapping[BillingPlanId, int]) -> PublishedCatalog:
        """Make the account agree with the rate card this repository publishes.

        Idempotent by name: a run against an account that already agrees writes
        nothing. A plan whose amount has changed is republished, because a
        price cannot be edited at the provider. A new one is created carrying the
        same lookup key and the old one is retired, so whatever resolves a plan
        by lookup key finds the current figure.

        Subscriptions already open are left on the price they were opened
        against. They hold a provider identifier rather than a lookup key, so
        nothing here reaches them, and that is the intended limit: moving an
        account onto a different figure changes what somebody is charged, and it
        is not something a deploy should do on the way past.

        Every plan named in `plan_prices` is published. A plan absent from it is
        left alone rather than assumed free, because a plan price is the one
        figure this command must never invent.
        """

        plan_price_cents = {plan: cents(nanos) for plan, nanos in plan_prices.items()}
        meters = self._meters()
        for line in USAGE_LINES:
            if line.meter_event_name not in meters:
                meters[line.meter_event_name] = self._create_meter(line)
        products = self._products()
        for plan_product in PLAN_LINES:
            if plan_product.plan in plan_price_cents and plan_product.product_id not in products:
                products.add(
                    self._create_product(
                        product_id=plan_product.product_id,
                        name=plan_product.product_name,
                        unit_label="",
                    )
                )
        for line in USAGE_LINES:
            if line.product_id not in products:
                products.add(
                    self._create_product(
                        product_id=line.product_id,
                        name=line.product_name,
                        unit_label=NANODOLLAR_UNIT_LABEL,
                    )
                )
        prices = self._prices()
        for plan_product in PLAN_LINES:
            if plan_product.plan not in plan_price_cents:
                continue
            amount_cents = plan_price_cents[plan_product.plan]
            published = prices.get(plan_product.price_lookup_key)
            if published is None:
                self._create_plan_price(plan_product, plan_price_cents=amount_cents)
            elif published.unit_amount != amount_cents:
                self._reprice_plan(plan_product, published, plan_price_cents=amount_cents)
        for line in USAGE_LINES:
            existing = prices.get(line.price_lookup_key)
            if existing is None:
                self._create_meter_price(line, meter_id=meters[line.meter_event_name])
                continue
            self._refuse_mismatch(existing, line, meter_id=meters[line.meter_event_name])
        return self.published(plan_prices=plan_prices)

    def _refuse_mismatch(self, price: _Price, line: UsageLine, *, meter_id: str) -> None:
        published = price.unit_amount_decimal or ""
        if not published or Decimal(published) != Decimal(NANODOLLAR_UNIT_AMOUNT):
            raise ConflictError(
                f"{line.price_lookup_key} is published at {published or 'no'} cents per unit, "
                f"not {NANODOLLAR_UNIT_AMOUNT}"
            )
        attached = price.recurring.meter if price.recurring is not None else None
        if attached != meter_id:
            raise ConflictError(
                f"{line.price_lookup_key} reads meter {attached or 'none'}, not {meter_id}"
            )

    def _create_meter(self, line: UsageLine) -> str:
        return read(
            _Meter,
            self.client,
            "POST",
            "/billing/meters",
            data=[
                ("display_name", f"{line.product_name} cost"),
                ("event_name", line.meter_event_name),
                ("default_aggregation[formula]", "sum"),
                ("customer_mapping[type]", "by_id"),
                ("customer_mapping[event_payload_key]", "stripe_customer_id"),
                ("value_settings[event_payload_key]", "value"),
            ],
        ).id

    def _create_product(self, *, product_id: str, name: str, unit_label: str) -> str:
        data: list[tuple[str, str]] = [("id", product_id), ("name", name)]
        if unit_label:
            data.append(("unit_label", unit_label))
        return read(_Product, self.client, "POST", "/products", data=data).id

    def _create_plan_price(self, line: PlanLine, *, plan_price_cents: int) -> str:
        return read(
            _Price,
            self.client,
            "POST",
            "/prices",
            data=[
                ("currency", BILLING_CURRENCY.lower()),
                ("product", line.product_id),
                ("lookup_key", line.price_lookup_key),
                ("unit_amount", str(plan_price_cents)),
                ("billing_scheme", "per_unit"),
                ("recurring[interval]", "month"),
                ("recurring[usage_type]", "licensed"),
            ],
        ).id

    def _reprice_plan(self, line: PlanLine, current: _Price, *, plan_price_cents: int) -> str:
        """Publish the current figure under the key, and retire the old price.

        The key moves first. Between the two calls the account holds a price
        nothing resolves to rather than two that both answer to one name, which
        is the ordering that fails safe if the second call does not happen.
        """

        created = read(
            _Price,
            self.client,
            "POST",
            "/prices",
            data=[
                ("currency", BILLING_CURRENCY.lower()),
                ("product", line.product_id),
                ("lookup_key", line.price_lookup_key),
                ("transfer_lookup_key", "true"),
                ("unit_amount", str(plan_price_cents)),
                ("billing_scheme", "per_unit"),
                ("recurring[interval]", "month"),
                ("recurring[usage_type]", "licensed"),
            ],
        ).id
        # Retired rather than deleted, which the provider does not offer. Open
        # subscriptions name it directly and go on billing against it.
        read(_Price, self.client, "POST", f"/prices/{current.id}", data=[("active", "false")])
        return created

    @staticmethod
    def _plan_price_disagreement(
        prices: dict[str, _Price],
        lookup_key: str,
        amount_cents: int,
    ) -> str:
        published = prices.get(lookup_key)
        if published is None or published.unit_amount == amount_cents:
            return ""
        return f"{published.unit_amount} cents monthly, licensed"

    def _create_meter_price(self, line: UsageLine, *, meter_id: str) -> str:
        return read(
            _Price,
            self.client,
            "POST",
            "/prices",
            data=[
                ("currency", BILLING_CURRENCY.lower()),
                ("product", line.product_id),
                ("lookup_key", line.price_lookup_key),
                ("unit_amount_decimal", NANODOLLAR_UNIT_AMOUNT),
                ("billing_scheme", "per_unit"),
                ("recurring[interval]", "month"),
                ("recurring[usage_type]", "metered"),
                ("recurring[meter]", meter_id),
            ],
        ).id

    def _meters(self) -> dict[str, str]:
        found: dict[str, str] = {}
        starting_after = ""
        while True:
            params: list[tuple[str, str]] = [("status", "active"), ("limit", "100")]
            if starting_after:
                params.append(("starting_after", starting_after))
            page = read(_MeterList, self.client, "GET", "/billing/meters", params=params)
            found.update({meter.event_name: meter.id for meter in page.data})
            if not page.has_more or not page.data:
                return found
            starting_after = page.data[-1].id

    def _products(self) -> set[str]:
        params: FormFields = [
            ("limit", "100"),
            *(("ids[]", line.product_id) for line in PLAN_LINES),
            *(("ids[]", line.product_id) for line in USAGE_LINES),
        ]
        listed = read(_ProductList, self.client, "GET", "/products", params=params)
        return {product.id for product in listed.data}

    def _prices(self) -> dict[str, _Price]:
        params: FormFields = [
            ("active", "true"),
            ("limit", "100"),
            *(("lookup_keys[]", line.price_lookup_key) for line in PLAN_LINES),
            *(("lookup_keys[]", key) for key in METERED_PRICE_LOOKUP_KEYS),
        ]
        listed = read(_PriceList, self.client, "GET", "/prices", params=params)
        return {price.lookup_key: price for price in listed.data if price.lookup_key}

    @staticmethod
    def _price_id(prices: dict[str, _Price], lookup_key: str) -> str:
        price = prices.get(lookup_key)
        return price.id if price is not None else ""


__all__ = [
    "METERED_PRICE_LOOKUP_KEYS",
    "NANODOLLAR_UNIT_AMOUNT",
    "NANODOLLAR_UNIT_LABEL",
    "NANOS_PER_CENT",
    "PLAN_LINES",
    "USAGE_LINES",
    "CatalogEntry",
    "CatalogObjectKind",
    "PlanLine",
    "PublishedCatalog",
    "StripeCatalog",
    "UsageLine",
    "cents",
    "plan_for_price_lookup_key",
    "plan_line",
    "subscription_price_lookup_keys",
]
