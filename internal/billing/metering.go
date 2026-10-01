package billing

import (
	"context"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

const (
	// MeteringPeriod is the UTC grid live containers' entries end on, so a
	// running container's usage reaches the ledger at most this late. The
	// balance does not wait for it: each pass writes the open interval as
	// accrued cost.
	MeteringPeriod = 15 * time.Minute
	// stoppedLookBack is how far before the previous pass a pass looks for
	// stopped containers. It covers stop transactions that committed after
	// the previous pass read: a stop's stopped_at is its transaction's
	// start, which precedes its commit by far less than this.
	stoppedLookBack = 5 * time.Minute
	// meteringWriteBatch bounds the containers one metering transaction
	// writes.
	meteringWriteBatch = 500
	cursorPruneBatch   = 5_000
	stopHostLost       = "host_lost"
	categoryImageBuild = "image-build"
)

// MeterResult counts one metering pass.
type MeterResult struct {
	// Skipped means another replica held the metering lock.
	Skipped bool
	// Containers is how many containers the pass read.
	Containers int
	// Entries is how many ledger entries it offered; entries already
	// written are skipped by the database.
	Entries int
	// Failed counts containers the pass could not price; they keep their
	// cursor and are retried next pass.
	Failed int
}

// entry is one priced interval of one container.
type entry struct {
	container uuid.UUID
	start     time.Time
	end       time.Time
	owner     uuid.UUID
	workspace uuid.UUID
	app       uuid.UUID
	workload  uuid.UUID
	category  string
	shape     Shape
	charge    Charge
}

// meteredContainer is one container a pass reads, live or recently
// stopped.
type meteredContainer struct {
	id, workspace, owner uuid.UUID
	app, workload        *uuid.UUID
	build                bool
	cpuMillis, memory    int64
	readyAt              time.Time
	stoppedAt            *time.Time
	stopReason           *string
	lastSeenAt           *time.Time
	billedThrough        *time.Time
	complete             bool
}

// containerShape is how a container prices. Containers do not record a GPU
// or a placement choice yet and hosts do not record who owns the machine,
// so every container prices as automatic CPU placement on the platform
// fleet until execution and compute record them.
func containerShape(c meteredContainer) Shape {
	return Shape{Owner: OwnerPlatformFleet, Class: ClassAuto, CPUMillis: c.cpuMillis, MemoryBytes: c.memory}
}

// containerPlan is what a pass writes for one container.
type containerPlan struct {
	entries  []entry
	through  time.Time
	complete bool
	accrued  int64
	advance  bool
}

// Meter advances every live and recently stopped container's cursor:
// stopped containers get their last entries, live ones the entries up to
// their host's last report on the metering grid, and the open remainder is
// written as accrued cost per account. Batches commit separately, so a
// failure keeps the batches before it.
func (b *Billing) Meter(ctx context.Context) (MeterResult, error) {
	conn, err := b.pool.Acquire(ctx)
	if err != nil {
		return MeterResult{}, fmt.Errorf("acquire metering connection: %w", err)
	}
	defer conn.Release()
	locked, err := New(conn).TryMeteringLock(ctx)
	if err != nil {
		return MeterResult{}, fmt.Errorf("try metering lock: %w", err)
	}
	if !locked {
		return MeterResult{Skipped: true}, nil
	}
	defer func() {
		// A failed unlock leaves the session lock held until the
		// connection closes; destroy it rather than return it to the pool.
		if err := New(conn).ReleaseMeteringLock(context.WithoutCancel(ctx)); err != nil {
			_ = conn.Conn().Close(context.WithoutCancel(ctx))
		}
	}()

	clock, err := b.queries.MeteringClock(ctx)
	if err != nil {
		return MeterResult{}, fmt.Errorf("read metering clock: %w", err)
	}
	now := clock.Now
	since := clock.StoppedSince.Add(-stoppedLookBack)
	containers, err := b.meteredContainers(ctx, since)
	if err != nil {
		return MeterResult{}, err
	}
	result := MeterResult{Containers: len(containers)}

	type accrual struct {
		nanos int64
		live  int
	}
	accrued := map[uuid.UUID]*accrual{}
	owners := map[uuid.UUID]bool{}
	var batch []meteredContainer
	var plans []containerPlan
	flush := func() error {
		if len(batch) == 0 {
			return nil
		}
		if err := b.writeMetering(ctx, batch, plans, owners); err != nil {
			return err
		}
		for _, p := range plans {
			result.Entries += len(p.entries)
		}
		batch, plans = batch[:0], plans[:0]
		clear(owners)
		return nil
	}
	for _, c := range containers {
		p, err := b.planContainer(c, now)
		if err != nil {
			result.Failed++
			b.logger.ErrorContext(ctx, "price container usage", "container_id", c.id, "error", err)
			continue
		}
		if c.stoppedAt == nil {
			a := accrued[c.owner]
			if a == nil {
				a = &accrual{}
				accrued[c.owner] = a
			}
			a.nanos += p.accrued
			a.live++
		}
		owners[c.owner] = true
		if !p.advance && len(p.entries) == 0 {
			continue
		}
		batch, plans = append(batch, c), append(plans, p)
		if len(batch) >= meteringWriteBatch {
			if err := flush(); err != nil {
				return result, err
			}
		}
	}
	if err := flush(); err != nil {
		return result, err
	}

	users := make([]uuid.UUID, 0, len(accrued))
	nanos := make([]int64, 0, len(accrued))
	live := make([]int32, 0, len(accrued))
	for user, a := range accrued {
		users, nanos, live = append(users, user), append(nanos, a.nanos), append(live, int32(a.live)) //nolint:gosec // Live containers per account are far below 2^31.
	}
	err = pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		if err := q.EnsureAccounts(ctx, EnsureAccountsParams{UserIds: users, TrialNanos: TrialNanos, TrialDays: TrialDays}); err != nil {
			return fmt.Errorf("ensure accounts: %w", err)
		}
		if err := q.SetAccrued(ctx, SetAccruedParams{UserIds: users, Accrued: nanos, Live: live}); err != nil {
			return fmt.Errorf("set accrued cost: %w", err)
		}
		if err := q.SetStoppedSince(ctx, now); err != nil {
			return fmt.Errorf("advance metering look-back: %w", err)
		}
		if _, err := q.PruneCursors(ctx, PruneCursorsParams{Before: now.Add(-2 * stoppedLookBack), RowLimit: cursorPruneBatch}); err != nil {
			return fmt.Errorf("prune cursors: %w", err)
		}
		return nil
	})
	if err != nil {
		return result, fmt.Errorf("finish metering pass: %w", err)
	}
	return result, nil
}

func (b *Billing) meteredContainers(ctx context.Context, since time.Time) ([]meteredContainer, error) {
	live, err := b.queries.LiveMeteredContainers(ctx)
	if err != nil {
		return nil, fmt.Errorf("read live containers: %w", err)
	}
	stopped, err := b.queries.StoppedMeteredContainers(ctx, &since)
	if err != nil {
		return nil, fmt.Errorf("read stopped containers: %w", err)
	}
	out := make([]meteredContainer, 0, len(live)+len(stopped))
	for _, r := range live {
		out = append(out, meteredContainer{
			id: r.ID, workspace: r.WorkspaceID, owner: r.OwnerID, app: r.AppID, workload: r.WorkloadID,
			build: r.ImageBuildID != nil, cpuMillis: r.CpuMillis, memory: r.MemoryBytes, readyAt: r.ReadyAt,
			stoppedAt: r.StoppedAt, stopReason: r.StopReason, lastSeenAt: r.LastSeenAt,
			billedThrough: r.BilledThrough, complete: r.Complete,
		})
	}
	for _, r := range stopped {
		out = append(out, meteredContainer{
			id: r.ID, workspace: r.WorkspaceID, owner: r.OwnerID, app: r.AppID, workload: r.WorkloadID,
			build: r.ImageBuildID != nil, cpuMillis: r.CpuMillis, memory: r.MemoryBytes, readyAt: r.ReadyAt,
			stoppedAt: r.StoppedAt, stopReason: r.StopReason, lastSeenAt: r.LastSeenAt,
			billedThrough: r.BilledThrough, complete: r.Complete,
		})
	}
	return out, nil
}

// planContainer decides one container's entries, its new cursor and its
// accrued cost at now. A container is billed from ready to stopped; one on a
// lost host ends at the host's last report, and a live one is never billed
// past it.
func (b *Billing) planContainer(c meteredContainer, now time.Time) (containerPlan, error) {
	start := c.readyAt
	if c.billedThrough != nil && c.billedThrough.After(start) {
		start = *c.billedThrough
	}
	shape := containerShape(c)
	if c.stoppedAt != nil {
		end := *c.stoppedAt
		if c.stopReason != nil && *c.stopReason == stopHostLost && c.lastSeenAt != nil && c.lastSeenAt.Before(end) {
			end = *c.lastSeenAt
		}
		entries, err := b.entries(c, shape, start, end)
		if err != nil {
			return containerPlan{}, err
		}
		return containerPlan{entries: entries, through: maxTime(start, end), complete: true, advance: true}, nil
	}
	horizon := start
	if c.lastSeenAt != nil {
		horizon = maxTime(start, minTime(now, *c.lastSeenAt))
	}
	closed := horizon.Truncate(MeteringPeriod)
	plan := containerPlan{through: start}
	if closed.After(start) {
		entries, err := b.entries(c, shape, start, closed)
		if err != nil {
			return containerPlan{}, err
		}
		plan.entries, plan.through, plan.advance = entries, closed, true
	}
	if horizon.After(plan.through) {
		open, err := b.entries(c, shape, plan.through, horizon)
		if err != nil {
			return containerPlan{}, err
		}
		for _, e := range open {
			plan.accrued += e.charge.Total()
		}
	}
	return plan, nil
}

// entries prices [start, end) in pieces that end on the metering grid and
// on published rate changes.
func (b *Billing) entries(c meteredContainer, shape Shape, start, end time.Time) ([]entry, error) {
	var out []entry
	cuts := b.rates.changesBetween(start, end)
	for from := start; from.Before(end); {
		to := minTime(from.Truncate(MeteringPeriod).Add(MeteringPeriod), end)
		for len(cuts) > 0 && !cuts[0].After(from) {
			cuts = cuts[1:]
		}
		if len(cuts) > 0 && cuts[0].Before(to) {
			to = cuts[0]
		}
		card, err := b.rates.cardAt(from)
		if err != nil {
			return nil, err
		}
		charge, err := card.price(shape, to.Sub(from))
		if err != nil {
			return nil, err
		}
		e := entry{
			container: c.id, start: from, end: to, owner: c.owner, workspace: c.workspace,
			shape: shape, charge: charge,
		}
		if c.app != nil {
			e.app = *c.app
		}
		if c.workload != nil {
			e.workload = *c.workload
		}
		if c.build {
			e.category = categoryImageBuild
		}
		out = append(out, e)
		from = to
	}
	return out, nil
}

// writeMetering commits one batch: accounts for every owner, the entries
// with their hourly totals, and the cursors.
func (b *Billing) writeMetering(ctx context.Context, batch []meteredContainer, plans []containerPlan, owners map[uuid.UUID]bool) error {
	var p InsertLedgerEntriesParams
	cursors := AdvanceCursorsParams{}
	for n, c := range batch {
		plan := plans[n]
		for _, e := range plan.entries {
			p.SourceIds = append(p.SourceIds, e.container)
			p.StartedAts = append(p.StartedAts, e.start)
			p.EndedAts = append(p.EndedAts, e.end)
			p.UserIds = append(p.UserIds, e.owner)
			p.WorkspaceIds = append(p.WorkspaceIds, e.workspace)
			p.AppIds = append(p.AppIds, e.app)
			p.WorkloadIds = append(p.WorkloadIds, e.workload)
			p.Categories = append(p.Categories, e.category)
			p.BillingOwners = append(p.BillingOwners, string(e.shape.Owner))
			p.RateClasses = append(p.RateClasses, string(e.shape.Class))
			p.GpuTypes = append(p.GpuTypes, string(e.shape.GPU))
			p.GpuCounts = append(p.GpuCounts, int32(e.shape.GPUCount)) //nolint:gosec // GPU counts are small.
			p.CpuMillis = append(p.CpuMillis, e.shape.CPUMillis)
			p.MemoryBytes = append(p.MemoryBytes, e.shape.MemoryBytes)
			p.PricingVersions = append(p.PricingVersions, e.charge.Version)
			p.ContainerNanos = append(p.ContainerNanos, e.charge.ContainerNanos)
			p.CpuNanos = append(p.CpuNanos, e.charge.CPUNanos)
			p.MemoryNanos = append(p.MemoryNanos, e.charge.MemoryNanos)
			p.GpuNanos = append(p.GpuNanos, e.charge.GPUNanos)
		}
		if plan.advance {
			cursors.Ids = append(cursors.Ids, c.id)
			cursors.Through = append(cursors.Through, plan.through)
			cursors.Complete = append(cursors.Complete, plan.complete)
		}
	}
	users := make([]uuid.UUID, 0, len(owners))
	for user := range owners {
		users = append(users, user)
	}
	err := pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		if err := q.EnsureAccounts(ctx, EnsureAccountsParams{UserIds: users, TrialNanos: TrialNanos, TrialDays: TrialDays}); err != nil {
			return fmt.Errorf("ensure accounts: %w", err)
		}
		if len(p.SourceIds) > 0 {
			if err := q.InsertLedgerEntries(ctx, p); err != nil {
				return fmt.Errorf("insert ledger entries: %w", err)
			}
		}
		if len(cursors.Ids) > 0 {
			if err := q.AdvanceCursors(ctx, cursors); err != nil {
				return fmt.Errorf("advance cursors: %w", err)
			}
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("write metering batch: %w", err)
	}
	return nil
}

func minTime(a, b time.Time) time.Time {
	if a.Before(b) {
		return a
	}
	return b
}

func maxTime(a, b time.Time) time.Time {
	if a.After(b) {
		return a
	}
	return b
}
