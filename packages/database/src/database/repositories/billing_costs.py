from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from database.tables.apps import AppTable, StubTable
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.identity import WorkspaceTable
from shared.billing_quotes import BilledDimension, LedgerComponent
from shared.errors import InvalidInputError
from shared.http.usage import UsageCostGroupKey
from sqlalchemy import (
    BigInteger,
    ColumnElement,
    and_,
    cast,
    extract,
    func,
    or_,
    select,
)
from sqlalchemy.orm import InstrumentedAttribute, Session

_GROUP_COLUMNS: dict[UsageCostGroupKey, tuple[InstrumentedAttribute[str], ...]] = {
    UsageCostGroupKey.App: (
        BillingLedgerSegmentTable.workspace_id,
        BillingLedgerSegmentTable.app_id,
    ),
    UsageCostGroupKey.Workload: (
        BillingLedgerSegmentTable.workspace_id,
        BillingLedgerSegmentTable.app_id,
        BillingLedgerSegmentTable.workload_id,
    ),
    UsageCostGroupKey.Task: (
        BillingLedgerSegmentTable.workspace_id,
        BillingLedgerSegmentTable.app_id,
        BillingLedgerSegmentTable.workload_id,
        BillingLedgerSegmentTable.task_id,
    ),
}
"""Every level carries the ids above it, so a row names its own place in the
product model. The whole tuple is what a row is grouped by and what distinguishes
it: a container that is not a task carries an empty `task_id`, so the deepest
column alone is shared by every such group.

Workspace leads every level because a page may cover several. Grouped by app
alone, usage that reached no app carries an empty `app_id` — real, and the same
empty key in every workspace, so two accounts' unattributed spend would sum into
one row that names neither."""


@dataclass(frozen=True, slots=True)
class LedgerCostCursor:
    """Where the previous page stopped, in the order the rows are read.

    Cost descending then the whole group key ascending. The key is the tuple the
    query grouped on, so the pair is total and a page boundary can neither repeat
    a row nor skip one.
    """

    cost_nanos: int
    group_key: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LedgerComponentTotal:
    """One resource's share of a row, and which meter it invoices under.

    Both bases summed together: a customer reading "this app used N core-seconds"
    means the capacity it was charged for, which is the floor it held plus
    whatever it burnt above that.
    """

    dimension: BilledDimension
    component: LedgerComponent
    quantity: Decimal
    cost_nanos: int


@dataclass(frozen=True, slots=True)
class LedgerCostRow:
    """What one group cost, and the resources that cost is made of.

    No resource total beside `components`: every one of them would be a second
    sum over the same rows, and two sums of one thing are two things to keep in
    agreement.
    """

    workspace_id: str
    workspace_name: str
    app_id: str
    app_name: str

    workload_id: str
    workload_name: str
    workload_kind: str
    task_id: str
    cost_nanos: int
    components: tuple[LedgerComponentTotal, ...]


@dataclass(frozen=True, slots=True)
class LedgerCostPage:
    rows: tuple[LedgerCostRow, ...]
    next: LedgerCostCursor | None


@dataclass(frozen=True, slots=True)
class LedgerCostBucketTotal:
    """What one interval of a window cost, on one invoice line.

    `index` counts whole bucket widths from the window's start, so the interval
    it stands for is arithmetic on the start the caller supplied rather than a
    timestamp this query decided. Only the intervals that carry cost come back;
    the intervals between them are the caller's to fill, which is the one place
    the width and the start are both already known.
    """

    index: int
    dimension: BilledDimension
    cost_nanos: int


@dataclass(frozen=True, slots=True)
class LedgerComponentBucketQuantity:
    """How much of one resource one app held inside one interval of a window.

    `index` counts whole interval widths from the window's start, so the span it
    stands for is arithmetic on the start the caller supplied rather than a
    timestamp this query decided. Only the intervals a workspace metered
    something in come back; the intervals between them are the caller's to fill,
    which is the one place the width and the start are both already known.

    Keyed by workspace and app together for the reason the cost grouping is: an
    account reads several workspaces at once, and an app id is unique while an
    absent one is the same empty key in every workspace.
    """

    workspace_id: str
    app_id: str
    index: int
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class BillingLedgerCostRepository:
    """What the frozen ledger adds up to, for the dashboard and for reconciliation.

    Three questions, one owner: what a workspace spent, broken down the way a
    customer reads it; what a payer spent over a window, keyed the way an invoice
    is; and how much of one resource an app held over a window, which is the same
    rows summed on `quantity` instead of on `cost_nanos`. They are the same rows
    summed on different columns, and summing them anywhere else would be a second
    answer to what something cost or used.

    Reads `billing_ledger_segments` and nothing else. The ledger is append-only
    and already carries the attribution, the component and the quantity each
    segment was priced from, so answering "which app cost what" needs no join and
    no recomputation from usage.

    A resource total is that component's rows summed and nothing else. One
    metering window writes a row per resource it used, so a column summed across
    all of them would count the window's processor-seconds, its gibibyte-seconds
    and its egress bytes into one meaningless figure.

    Segments are placed by `segment_started_at`, the instant they were metered
    over — never by when the row was written, which is the pricer's clock rather
    than the customer's.
    """

    session: Session

    def window_cost_nanos(
        self,
        *,
        workspace_ids: Sequence[str],
        start: datetime,
        end: datetime,
        app_id: str | None = None,
        workload_id: str | None = None,
    ) -> int:
        total = self.session.scalars(
            select(func.coalesce(func.sum(BillingLedgerSegmentTable.cost_nanos), 0)).where(
                *_window(
                    workspace_ids=workspace_ids,
                    start=start,
                    end=end,
                    app_id=app_id,
                    workload_id=workload_id,
                )
            )
        ).one()
        return int(total)

    def bucket_totals(
        self,
        *,
        workspace_ids: Sequence[str],
        start: datetime,
        end: datetime,
        width_seconds: int,
        app_id: str | None = None,
        workload_id: str | None = None,
    ) -> tuple[LedgerCostBucketTotal, ...]:
        """What each interval of a window cost, per invoice line.

        Placed into intervals by whole seconds elapsed from `start`, never by
        `date_trunc` or `func.date`: those read the session `TimeZone`, which
        nothing here sets, so the day a bar belonged to would depend on which
        connection answered the request.

        Grouped by dimension as well as by interval because that is the
        granularity an invoice line has, and it costs the same read — the rows
        are already being scanned to total them.
        """

        if width_seconds < 1:
            raise InvalidInputError("a cost interval must be at least one second wide")
        # Seconds against seconds, rather than an interval between two instants:
        # the elapsed span is what both engines compute the same way, where
        # subtracting one timestamp column from another is a `- interval` overload
        # on one and string arithmetic on the other.
        elapsed = extract("epoch", BillingLedgerSegmentTable.segment_started_at) - int(
            start.timestamp()
        )
        index = cast(func.floor(elapsed / width_seconds), BigInteger)
        found = self.session.execute(
            select(
                index,
                BillingLedgerSegmentTable.dimension,
                func.coalesce(func.sum(BillingLedgerSegmentTable.cost_nanos), 0),
            )
            .where(
                *_window(
                    workspace_ids=workspace_ids,
                    start=start,
                    end=end,
                    app_id=app_id,
                    workload_id=workload_id,
                )
            )
            .group_by(index, BillingLedgerSegmentTable.dimension)
            .order_by(index.asc(), BillingLedgerSegmentTable.dimension.asc())
        ).all()
        return tuple(
            LedgerCostBucketTotal(
                index=int(row[0]),
                dimension=BilledDimension(row[1]),
                cost_nanos=int(row[2]),
            )
            for row in found
        )

    def component_bucket_quantities(
        self,
        *,
        workspace_ids: Sequence[str],
        component: LedgerComponent,
        start: datetime,
        end: datetime,
        width_seconds: int,
    ) -> tuple[LedgerComponentBucketQuantity, ...]:
        """How much of one resource each app held, interval by interval.

        One component and nothing else, because `quantity` means a different unit
        in every one of them: processor-seconds, gibibyte-seconds and card-seconds
        summed together are a figure in no unit at all.

        Both bases are summed, matching what a customer is charged and shown: the
        capacity a placement held plus whatever it burnt above that. Filtering to
        one basis would report a container that never sent a measurement as
        holding nothing.

        Placed into intervals by whole seconds elapsed from `start`, never by
        `date_trunc` or `func.date`: those read the session `TimeZone`, which
        nothing here sets, so the interval a segment belonged to would depend on
        which connection answered the request.
        """

        if width_seconds < 1:
            raise InvalidInputError("a resource interval must be at least one second wide")
        if not workspace_ids:
            return ()
        # Seconds against seconds, for the reason `bucket_totals` gives: the
        # elapsed span is what both engines compute the same way.
        elapsed = extract("epoch", BillingLedgerSegmentTable.segment_started_at) - int(
            start.timestamp()
        )
        index = cast(func.floor(elapsed / width_seconds), BigInteger)
        found = self.session.execute(
            select(
                BillingLedgerSegmentTable.workspace_id,
                BillingLedgerSegmentTable.app_id,
                index,
                func.coalesce(func.sum(BillingLedgerSegmentTable.quantity), 0),
            )
            .where(
                *_window(
                    workspace_ids=workspace_ids,
                    start=start,
                    end=end,
                    app_id=None,
                    workload_id=None,
                ),
                BillingLedgerSegmentTable.component == component.value,
            )
            .group_by(
                BillingLedgerSegmentTable.workspace_id,
                BillingLedgerSegmentTable.app_id,
                index,
            )
        ).all()
        return tuple(
            LedgerComponentBucketQuantity(
                workspace_id=str(row[0]),
                app_id=str(row[1]),
                index=int(row[2]),
                quantity=Decimal(row[3]),
            )
            for row in found
        )

    def account_dimension_totals(
        self, *, user_id: str, start: datetime, end: datetime
    ) -> Mapping[BilledDimension, int]:
        """What one payer's usage cost over a window, per invoice line.

        Keyed by dimension because that is what the provider meters and invoices
        — the components a dimension breaks into are this platform's own
        granularity and have no counterpart on a bill. Placed by
        `segment_started_at`, the same instant the allowance counter uses and the
        one `(owner_user_id, segment_started_at)` indexes; a meter event is
        stamped at its span's start instead, and the two differ only for a span
        split across a published rate change, which is the boundary a comparison
        over this may disagree at.
        """

        found = self.session.execute(
            select(
                BillingLedgerSegmentTable.dimension,
                func.coalesce(func.sum(BillingLedgerSegmentTable.cost_nanos), 0),
            )
            .where(
                BillingLedgerSegmentTable.owner_user_id == user_id,
                BillingLedgerSegmentTable.segment_started_at >= start,
                BillingLedgerSegmentTable.segment_started_at < end,
            )
            .group_by(BillingLedgerSegmentTable.dimension)
        ).all()
        return {BilledDimension(row[0]): int(row[1]) for row in found}

    def page(
        self,
        *,
        workspace_ids: Sequence[str],
        start: datetime,
        end: datetime,
        group_by: UsageCostGroupKey,
        limit: int,
        app_id: str | None = None,
        workload_id: str | None = None,
        cursor: LedgerCostCursor | None = None,
    ) -> LedgerCostPage:
        columns = _GROUP_COLUMNS[group_by]
        cost = func.coalesce(func.sum(BillingLedgerSegmentTable.cost_nanos), 0).label("cost_nanos")
        statement = (
            select(
                *columns,
                cost,
            )
            .where(
                *_window(
                    workspace_ids=workspace_ids,
                    start=start,
                    end=end,
                    app_id=app_id,
                    workload_id=workload_id,
                )
            )
            .group_by(*columns)
            .order_by(cost.desc(), *(column.asc() for column in columns))
            # One more than asked for, so whether a further page exists is
            # answered by reading rather than by a second count that could
            # disagree with it.
            .limit(limit + 1)
        )
        if cursor is not None:
            statement = statement.having(_after(cost, columns, cursor))
        found = self.session.execute(statement).all()
        keys = [tuple(str(value) for value in row[: len(columns)]) for row in found[:limit]]
        components = self._components(
            workspace_ids=workspace_ids,
            start=start,
            end=end,
            columns=columns,
            keys=keys,
            app_id=app_id,
            workload_id=workload_id,
        )
        names = self._names(keys)
        rows = tuple(
            _cost_row(
                key=key,
                cost_nanos=int(row[len(columns)]),
                components=components.get(key, ()),
                names=names,
            )
            for key, row in zip(keys, found, strict=False)
        )
        return LedgerCostPage(
            rows=rows,
            next=(
                LedgerCostCursor(cost_nanos=rows[-1].cost_nanos, group_key=keys[-1])
                if len(found) > limit and rows
                else None
            ),
        )

    def _components(
        self,
        *,
        workspace_ids: Sequence[str],
        start: datetime,
        end: datetime,
        columns: tuple[InstrumentedAttribute[str], ...],
        keys: Sequence[tuple[str, ...]],
        app_id: str | None,
        workload_id: str | None,
    ) -> dict[tuple[str, ...], tuple[LedgerComponentTotal, ...]]:
        """Each row's components, restricted to the rows the page carries.

        A second statement rather than one grouped by component as well, because
        the page is ordered by what a group cost in total and that total only
        exists once the components are already summed together.
        """

        if not keys:
            return {}
        deepest = columns[-1]
        totals: dict[tuple[str, ...], list[LedgerComponentTotal]] = {}
        found = self.session.execute(
            select(
                *columns,
                BillingLedgerSegmentTable.dimension,
                BillingLedgerSegmentTable.component,
                func.coalesce(func.sum(BillingLedgerSegmentTable.quantity), 0),
                func.coalesce(func.sum(BillingLedgerSegmentTable.cost_nanos), 0),
            )
            .where(
                *_window(
                    workspace_ids=workspace_ids,
                    start=start,
                    end=end,
                    app_id=app_id,
                    workload_id=workload_id,
                ),
                deepest.in_([key[-1] for key in keys]),
            )
            .group_by(
                *columns,
                BillingLedgerSegmentTable.dimension,
                BillingLedgerSegmentTable.component,
            )
            .order_by(
                BillingLedgerSegmentTable.dimension.asc(),
                BillingLedgerSegmentTable.component.asc(),
            )
        ).all()
        for row in found:
            key = tuple(str(value) for value in row[: len(columns)])
            dimension, component, quantity, cost_nanos = row[len(columns) :]
            totals.setdefault(key, []).append(
                LedgerComponentTotal(
                    dimension=BilledDimension(dimension),
                    component=LedgerComponent(component),
                    quantity=Decimal(quantity),
                    cost_nanos=int(cost_nanos),
                )
            )
        return {key: tuple(values) for key, values in totals.items()}

    def _names(self, keys: Sequence[tuple[str, ...]]) -> _ResolvedNames:
        """Human names for the ids the page carries.

        Resolved here rather than in the browser: a customer surface speaks in
        names, and assembling them from a second unrelated list is how a row ends
        up labelled with the wrong app. Absent where the app or workload has since
        been deleted — the cost stays, and the row says so by carrying an id and
        no name rather than inventing one.
        """

        workspace_ids = {key[0] for key in keys if key[0]}
        app_ids = {key[1] for key in keys if len(key) > 1 and key[1]}
        workload_ids = {key[2] for key in keys if len(key) > 2 and key[2]}
        workspaces = (
            {
                str(row[0]): str(row[1])
                for row in self.session.execute(
                    select(WorkspaceTable.id, WorkspaceTable.name).where(
                        WorkspaceTable.id.in_(workspace_ids)
                    )
                ).all()
            }
            if workspace_ids
            else {}
        )
        apps = (
            {
                str(row[0]): str(row[1])
                for row in self.session.execute(
                    select(AppTable.id, AppTable.name).where(AppTable.id.in_(app_ids))
                ).all()
            }
            if app_ids
            else {}
        )
        workloads = (
            {
                str(row[0]): (str(row[1]), str(row[2]))
                for row in self.session.execute(
                    select(StubTable.id, StubTable.name, StubTable.type).where(
                        StubTable.id.in_(workload_ids)
                    )
                ).all()
            }
            if workload_ids
            else {}
        )
        return _ResolvedNames(workspaces=workspaces, apps=apps, workloads=workloads)


@dataclass(frozen=True, slots=True)
class _ResolvedNames:
    workspaces: dict[str, str]
    apps: dict[str, str]
    workloads: dict[str, tuple[str, str]]


def _after(
    cost: ColumnElement[int],
    columns: tuple[InstrumentedAttribute[str], ...],
    cursor: LedgerCostCursor,
) -> ColumnElement[bool]:
    """The groups that fall after the cursor in `(cost desc, key asc)` order.

    Spelled out as nested comparisons rather than a row constructor because the
    two directions differ, and a row comparison orders every member the same way.
    """

    if len(cursor.group_key) != len(columns):
        raise InvalidInputError("a usage cost cursor belongs to the grouping that issued it")
    pairs = list(zip(columns, cursor.group_key, strict=True))
    column, value = pairs[-1]
    key_after: ColumnElement[bool] = column > value
    for column, value in reversed(pairs[:-1]):
        key_after = or_(column > value, and_(column == value, key_after))
    return or_(cost < cursor.cost_nanos, and_(cost == cursor.cost_nanos, key_after))


def _window(
    *,
    workspace_ids: Sequence[str],
    start: datetime,
    end: datetime,
    app_id: str | None,
    workload_id: str | None,
) -> tuple[ColumnElement[bool], ...]:
    predicates: tuple[ColumnElement[bool], ...] = (
        BillingLedgerSegmentTable.workspace_id.in_(workspace_ids),
        BillingLedgerSegmentTable.segment_started_at >= start,
        BillingLedgerSegmentTable.segment_started_at < end,
    )
    if app_id is not None:
        predicates = (*predicates, BillingLedgerSegmentTable.app_id == app_id)
    if workload_id is not None:
        predicates = (*predicates, BillingLedgerSegmentTable.workload_id == workload_id)
    return predicates


def _cost_row(
    *,
    key: tuple[str, ...],
    cost_nanos: int,
    components: tuple[LedgerComponentTotal, ...],
    names: _ResolvedNames,
) -> LedgerCostRow:
    # The key carries the ids above its level and stops there, so a shallower
    # grouping leaves the levels below it empty rather than absent.
    workspace_id, app_id, workload_id, task_id = (*key, "", "")[:4]
    workload_name, workload_kind = names.workloads.get(workload_id, ("", ""))
    return LedgerCostRow(
        workspace_id=workspace_id,
        workspace_name=names.workspaces.get(workspace_id, ""),
        app_id=app_id,
        app_name=names.apps.get(app_id, ""),
        workload_id=workload_id,
        workload_name=workload_name,
        workload_kind=workload_kind,
        task_id=task_id,
        cost_nanos=cost_nanos,
        components=components,
    )


__all__ = [
    "BillingLedgerCostRepository",
    "LedgerComponentBucketQuantity",
    "LedgerComponentTotal",
    "LedgerCostBucketTotal",
    "LedgerCostCursor",
    "LedgerCostPage",
    "LedgerCostRow",
]
