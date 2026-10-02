package billing

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

const (
	// MeteringPeriod is the UTC grid entries end on, so a running
	// container's or a stored source's usage reaches the ledger at most this
	// late. The balance does not wait for compute: each pass writes live
	// containers' open intervals as accrued cost.
	MeteringPeriod = 15 * time.Minute
	// stoppedLookBack is how far before the previous pass a pass looks for
	// stopped containers. It covers stop transactions that committed after
	// the previous pass read: a stop's stopped_at is its transaction's
	// start, which precedes its commit by far less than this.
	stoppedLookBack = 5 * time.Minute
	// storageGone is how long a storage source's cursor outlives the source.
	storageGone = 24 * time.Hour
	// meteringWriteBatch bounds the sources one metering transaction
	// writes.
	meteringWriteBatch = 500
	cursorPruneBatch   = 5_000
	stopHostLost       = "host_lost"
	categoryImageBuild = "image-build"

	kindContainer = "container"
	kindVolume    = "volume"
	kindArtifacts = "artifacts"
	kindDisk      = "disk"
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
	// Failed counts sources the pass could not price; they keep their
	// cursor and are retried next pass.
	Failed int
}

// source is one thing usage is metered for: a container, a volume, one
// app's artifacts or a disk.
type source struct {
	kind      string
	id        uuid.UUID
	owner     uuid.UUID
	workspace uuid.UUID
	app       *uuid.UUID
	workload  *uuid.UUID
	category  string
	// price prices holding the source for d under card.
	price func(card RateCard, d time.Duration) (Charge, error)
	// shape is what a container prices; storage sources leave it zero.
	shape Shape
}

// entry is one priced interval of one source.
type entry struct {
	start, end time.Time
	charge     Charge
	// billed is the container shape the entry charged, when measured use
	// exceeded the reservation; nil charges the source's shape.
	billed *Shape
}

// sourcePlan is what a pass writes for one source.
type sourcePlan struct {
	src      source
	entries  []entry
	through  time.Time
	complete bool
	accrued  int64
	advance  bool
}

// meteredContainer is one container a pass reads, live or recently
// stopped.
type meteredContainer struct {
	id, workspace, owner uuid.UUID
	app, workload        *uuid.UUID
	build                bool
	shape                Shape
	readyAt              time.Time
	stoppedAt            *time.Time
	stopReason           *string
	lastSeenAt           *time.Time
	billedThrough        *time.Time
}

// Meter advances every metered source's cursor: stopped containers get
// their last entries, live ones the entries up to their host's last report
// on the metering grid, and stored volumes, artifacts and disks the
// quarter-hours that closed. The open remainder of live containers is
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
	containers, err := b.meteredContainers(ctx, clock.StoppedSince.Add(-stoppedLookBack))
	if err != nil {
		return MeterResult{}, err
	}
	result := MeterResult{Containers: len(containers)}

	type accrual struct {
		nanos int64
		live  int
	}
	accrued := map[uuid.UUID]*accrual{}
	var batch []sourcePlan
	flush := func() error {
		if len(batch) == 0 {
			return nil
		}
		if err := b.writeMetering(ctx, batch); err != nil {
			return err
		}
		for _, p := range batch {
			result.Entries += len(p.entries)
		}
		batch = batch[:0]
		return nil
	}
	add := func(p sourcePlan) error {
		if !p.advance && len(p.entries) == 0 {
			return nil
		}
		batch = append(batch, p)
		if len(batch) >= meteringWriteBatch {
			return flush()
		}
		return nil
	}
	plans := make([]sourcePlan, 0, len(containers))
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
		plans = append(plans, p)
	}
	if err := b.chargeMeasuredUse(ctx, plans); err != nil {
		return result, err
	}
	for _, p := range plans {
		if err := add(p); err != nil {
			return result, err
		}
	}
	storage, err := b.storageSources(ctx)
	if err != nil {
		return result, err
	}
	for _, s := range storage {
		p, err := b.planStorage(s, now)
		if err != nil {
			result.Failed++
			b.logger.ErrorContext(ctx, "price storage usage", "kind", s.src.kind, "source_id", s.src.id, "error", err)
			continue
		}
		if err := add(p); err != nil {
			return result, err
		}
	}
	if err := flush(); err != nil {
		return result, err
	}
	egress, err := b.meterEgress(ctx, now)
	result.Entries += egress
	if err != nil {
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
		if _, err := q.PruneCursors(ctx, PruneCursorsParams{
			CompleteBefore: now.Add(-2 * stoppedLookBack), GoneBefore: now.Add(-storageGone), RowLimit: cursorPruneBatch,
		}); err != nil {
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
	// A container that stopped between the two reads is in both; its
	// stopped row wins, so it is written once.
	stoppedIDs := make(map[uuid.UUID]bool, len(stopped))
	for _, r := range stopped {
		stoppedIDs[r.ID] = true
	}
	out := make([]meteredContainer, 0, len(live)+len(stopped))
	for _, r := range live {
		if stoppedIDs[r.ID] {
			continue
		}
		out = append(out, meteredContainer{
			id: r.ID, workspace: r.WorkspaceID, owner: r.OwnerID, app: r.AppID, workload: r.WorkloadID,
			build: r.ImageBuildID != nil, readyAt: r.ReadyAt, stoppedAt: r.StoppedAt, stopReason: r.StopReason,
			lastSeenAt: r.LastSeenAt, billedThrough: r.BilledThrough,
			shape: Shape{
				Owner: BillingOwner(r.BillingOwner), Class: RateClass(r.RateClass), GPU: GPUType(r.GpuType),
				GPUCount: int(r.GpuCount), CPUMillis: r.CpuMillis, MemoryBytes: r.MemoryBytes,
			},
		})
	}
	for _, r := range stopped {
		if r.Complete {
			continue
		}
		out = append(out, meteredContainer{
			id: r.ID, workspace: r.WorkspaceID, owner: r.OwnerID, app: r.AppID, workload: r.WorkloadID,
			build: r.ImageBuildID != nil, readyAt: r.ReadyAt, stoppedAt: r.StoppedAt, stopReason: r.StopReason,
			lastSeenAt: r.LastSeenAt, billedThrough: r.BilledThrough,
			shape: Shape{
				Owner: BillingOwner(r.BillingOwner), Class: RateClass(r.RateClass), GPU: GPUType(r.GpuType),
				GPUCount: int(r.GpuCount), CPUMillis: r.CpuMillis, MemoryBytes: r.MemoryBytes,
			},
		})
	}
	return out, nil
}

// planContainer decides one container's entries, its new cursor and its
// accrued cost at now. A container is billed from ready to stopped; one on a
// lost host ends at the host's last report, and a live one is never billed
// past it.
func (b *Billing) planContainer(c meteredContainer, now time.Time) (sourcePlan, error) {
	shape := c.shape
	src := source{
		kind: kindContainer, id: c.id, owner: c.owner, workspace: c.workspace, app: c.app, workload: c.workload, shape: shape,
		price: func(card RateCard, d time.Duration) (Charge, error) { return card.price(shape, d) },
	}
	if c.build {
		src.category = categoryImageBuild
	}
	start := c.readyAt
	if c.billedThrough != nil && c.billedThrough.After(start) {
		start = *c.billedThrough
	}
	if c.stoppedAt != nil {
		end := *c.stoppedAt
		if c.stopReason != nil && *c.stopReason == stopHostLost && c.lastSeenAt != nil && c.lastSeenAt.Before(end) {
			end = *c.lastSeenAt
		}
		entries, err := b.entries(src, start, end)
		if err != nil {
			return sourcePlan{}, err
		}
		return sourcePlan{src: src, entries: entries, through: maxTime(start, end), complete: true, advance: true}, nil
	}
	horizon := start
	if c.lastSeenAt != nil {
		horizon = maxTime(start, minTime(now, *c.lastSeenAt))
	}
	closed := horizon.Truncate(MeteringPeriod)
	plan := sourcePlan{src: src, through: start}
	if closed.After(start) {
		entries, err := b.entries(src, start, closed)
		if err != nil {
			return sourcePlan{}, err
		}
		plan.entries, plan.through, plan.advance = entries, closed, true
	}
	if horizon.After(plan.through) {
		open, err := b.entries(src, plan.through, horizon)
		if err != nil {
			return sourcePlan{}, err
		}
		for _, e := range open {
			plan.accrued += e.charge.Total()
		}
	}
	return plan, nil
}

// storageSource is a stored source with where its billing starts and ends.
type storageSource struct {
	src     source
	from    time.Time
	cursor  *time.Time
	deleted *time.Time
}

// storageSources reads every volume, disk and app's artifacts with their
// size now. Sizes are sampled when a quarter-hour closes: volumes are
// measured by storage's sweep, artifacts and disks are recorded exactly.
// A source whose owner is in an unfunded retention period stores free.
func (b *Billing) storageSources(ctx context.Context) ([]storageSource, error) {
	var out []storageSource
	volumes, err := b.queries.MeteredVolumes(ctx)
	if err != nil {
		return nil, fmt.Errorf("read volumes: %w", err)
	}
	for _, v := range volumes {
		bytes, waived := v.SizeBytes, v.Waived
		out = append(out, storageSource{
			src: source{kind: kindVolume, id: v.ID, owner: v.OwnerID, workspace: v.WorkspaceID, category: kindVolume,
				price: volumePrice(bytes, waived)},
			from: v.CreatedAt, cursor: v.BilledThrough, deleted: v.DeletedAt,
		})
	}
	artifacts, err := b.queries.MeteredArtifacts(ctx)
	if err != nil {
		return nil, fmt.Errorf("read artifacts: %w", err)
	}
	for _, a := range artifacts {
		out = append(out, storageSource{
			src: source{kind: kindArtifacts, id: a.SourceID, owner: a.OwnerID, workspace: a.WorkspaceID, app: a.AppID,
				category: kindArtifacts, price: volumePrice(a.StoredBytes, a.Waived)},
			from: a.FirstStoredAt, cursor: a.BilledThrough,
		})
	}
	disks, err := b.queries.MeteredDisks(ctx)
	if err != nil {
		return nil, fmt.Errorf("read disks: %w", err)
	}
	for _, d := range disks {
		stored, attached, waived := d.StoredBytes, int64(0), d.Waived
		if d.Held {
			attached = d.SizeBytes
		}
		out = append(out, storageSource{
			src: source{kind: kindDisk, id: d.ID, owner: d.OwnerID, workspace: d.WorkspaceID, category: kindDisk,
				price: func(card RateCard, dur time.Duration) (Charge, error) {
					if card.Disk == nil {
						return Charge{}, errors.New("no disk rate was published then")
					}
					c := Charge{Version: card.Version, StoredBytes: stored, AttachedBytes: attached}
					if !waived {
						c.StorageNanos = storageCost(card.Disk.StoredGiBMonth, stored, dur)
						c.AttachedNanos = storageCost(card.Disk.AttachedGiBMonth, attached, dur)
					}
					return c, nil
				}},
			from: d.CreatedAt, cursor: d.BilledThrough, deleted: d.DeletedAt,
		})
	}
	return out, nil
}

func volumePrice(bytes int64, waived bool) func(RateCard, time.Duration) (Charge, error) {
	return func(card RateCard, d time.Duration) (Charge, error) {
		c := Charge{Version: card.Version, StoredBytes: bytes}
		if !waived {
			c.StorageNanos = storageCost(card.Platform.VolumeGiBMonth, bytes, d)
		}
		return c, nil
	}
}

// planStorage writes a stored source's closed quarter-hours, through its
// deletion when it was deleted.
func (b *Billing) planStorage(s storageSource, now time.Time) (sourcePlan, error) {
	start := s.from
	if s.cursor != nil && s.cursor.After(start) {
		start = *s.cursor
	}
	end, complete := now.Truncate(MeteringPeriod), false
	if s.deleted != nil {
		end, complete = *s.deleted, true
	}
	plan := sourcePlan{src: s.src, through: start}
	if !end.After(start) {
		plan.advance = complete && s.cursor == nil
		return plan, nil
	}
	entries, err := b.entries(s.src, start, end)
	if err != nil {
		return sourcePlan{}, err
	}
	plan.entries, plan.through, plan.complete, plan.advance = entries, end, complete, true
	return plan, nil
}

// entries prices [start, end) of src in pieces that end on the metering
// grid and on published rate changes.
func (b *Billing) entries(src source, start, end time.Time) ([]entry, error) {
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
		charge, err := src.price(card, to.Sub(from))
		if err != nil {
			return nil, err
		}
		out = append(out, entry{start: from, end: to, charge: charge})
		from = to
	}
	return out, nil
}

// writeMetering commits one batch: accounts for every owner, the entries
// with their hourly totals, and the cursors.
func (b *Billing) writeMetering(ctx context.Context, batch []sourcePlan) error {
	users, p, cursors := ledgerParams(batch)
	err := pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		return writeLedger(ctx, b.queries.WithTx(tx), users, p, cursors)
	})
	if err != nil {
		return fmt.Errorf("write metering batch: %w", err)
	}
	return nil
}

// ledgerParams lays a batch out as the columns the insert takes.
func ledgerParams(batch []sourcePlan) ([]uuid.UUID, InsertLedgerEntriesParams, AdvanceCursorsParams) {
	var p InsertLedgerEntriesParams
	var cursors AdvanceCursorsParams
	owners := map[uuid.UUID]bool{}
	for _, plan := range batch {
		src := plan.src
		owners[src.owner] = true
		for _, e := range plan.entries {
			p.SourceKinds = append(p.SourceKinds, src.kind)
			p.SourceIds = append(p.SourceIds, src.id)
			p.StartedAts = append(p.StartedAts, e.start)
			p.EndedAts = append(p.EndedAts, e.end)
			p.UserIds = append(p.UserIds, src.owner)
			p.WorkspaceIds = append(p.WorkspaceIds, src.workspace)
			p.AppIds = append(p.AppIds, orNil(src.app))
			p.WorkloadIds = append(p.WorkloadIds, orNil(src.workload))
			p.Categories = append(p.Categories, src.category)
			shape := src.shape
			if e.billed != nil {
				shape = *e.billed
			}
			owner, class := shape.Owner, shape.Class
			if owner == "" {
				owner, class = OwnerPlatformFleet, ClassAuto
			}
			p.BillingOwners = append(p.BillingOwners, string(owner))
			p.RateClasses = append(p.RateClasses, string(class))
			p.GpuTypes = append(p.GpuTypes, string(shape.GPU))
			p.GpuCounts = append(p.GpuCounts, int32(shape.GPUCount)) //nolint:gosec // GPU counts are small.
			p.CpuMillis = append(p.CpuMillis, shape.CPUMillis)
			p.MemoryBytes = append(p.MemoryBytes, shape.MemoryBytes)
			p.PricingVersions = append(p.PricingVersions, e.charge.Version)
			p.ContainerNanos = append(p.ContainerNanos, e.charge.ContainerNanos)
			p.CpuNanos = append(p.CpuNanos, e.charge.CPUNanos)
			p.MemoryNanos = append(p.MemoryNanos, e.charge.MemoryNanos)
			p.GpuNanos = append(p.GpuNanos, e.charge.GPUNanos)
			p.StoredBytes = append(p.StoredBytes, e.charge.StoredBytes)
			p.AttachedBytes = append(p.AttachedBytes, e.charge.AttachedBytes)
			p.StorageNanos = append(p.StorageNanos, e.charge.StorageNanos)
			p.AttachedNanos = append(p.AttachedNanos, e.charge.AttachedNanos)
			p.EgressBytes = append(p.EgressBytes, e.charge.EgressBytes)
			p.EgressNanos = append(p.EgressNanos, e.charge.EgressNanos)
		}
		if plan.advance {
			cursors.Kinds = append(cursors.Kinds, src.kind)
			cursors.Ids = append(cursors.Ids, src.id)
			cursors.Through = append(cursors.Through, plan.through)
			cursors.Complete = append(cursors.Complete, plan.complete)
		}
	}
	users := make([]uuid.UUID, 0, len(owners))
	for user := range owners {
		users = append(users, user)
	}
	return users, p, cursors
}

// writeLedger writes entries and cursors in the caller's transaction,
// creating the accounts they charge first.
func writeLedger(ctx context.Context, q *Queries, users []uuid.UUID, p InsertLedgerEntriesParams, cursors AdvanceCursorsParams) error {
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
}

// Egress is internet traffic the edge sent for a workload.
type Egress struct {
	Workspace uuid.UUID
	App       *uuid.UUID
	Workload  *uuid.UUID
	Bytes     int64
	At        time.Time
}

// egressLag is how long after a quarter-hour closes the edge may still add
// to it.
const egressLag = 5 * time.Minute

// RecordEgress adds traffic to its workload's quarter-hour. The edge calls
// it with what it counted since its last flush.
func RecordEgress(ctx context.Context, db DBTX, e Egress) error {
	if e.Bytes <= 0 {
		return nil
	}
	err := New(db).RecordEgress(ctx, RecordEgressParams{
		WorkspaceID: e.Workspace, AppID: orNil(e.App), WorkloadID: orNil(e.Workload),
		Quarter: e.At.UTC().Truncate(MeteringPeriod), Bytes: e.Bytes,
	})
	if err != nil {
		return fmt.Errorf("record egress: %w", err)
	}
	return nil
}

// meterEgress prices every closed quarter of egress into the ledger and
// removes it, one batch per transaction.
func (b *Billing) meterEgress(ctx context.Context, now time.Time) (int, error) {
	written := 0
	for {
		n, err := b.meterEgressBatch(ctx, now)
		written += n
		if err != nil || n < meteringWriteBatch {
			return written, err
		}
	}
}

func (b *Billing) meterEgressBatch(ctx context.Context, now time.Time) (int, error) {
	n := 0
	err := pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		rows, err := q.ClosedEgress(ctx, ClosedEgressParams{Before: now.Add(-egressLag - MeteringPeriod), RowLimit: meteringWriteBatch})
		if err != nil {
			return fmt.Errorf("read closed egress: %w", err)
		}
		n = len(rows)
		if n == 0 {
			return nil
		}
		var batch []sourcePlan
		var del DeleteEgressParams
		for _, r := range rows {
			card, err := b.rates.cardAt(r.Quarter)
			if err != nil {
				return err
			}
			src := source{kind: "egress", id: r.WorkspaceID, owner: r.OwnerID, workspace: r.WorkspaceID}
			if r.AppID != uuid.Nil {
				app := r.AppID
				src.app, src.id = &app, app
			}
			if r.WorkloadID != uuid.Nil {
				workload := r.WorkloadID
				src.workload, src.id = &workload, workload
			}
			charge := Charge{Version: card.Version, EgressBytes: r.Bytes, EgressNanos: costOver(card.Platform.EgressGiB, r.Bytes, bytesPerGiB, 1, 1)}
			batch = append(batch, sourcePlan{src: src, entries: []entry{{start: r.Quarter, end: r.Quarter.Add(MeteringPeriod), charge: charge}}})
			del.WorkspaceIds = append(del.WorkspaceIds, r.WorkspaceID)
			del.AppIds = append(del.AppIds, r.AppID)
			del.WorkloadIds = append(del.WorkloadIds, r.WorkloadID)
			del.Quarters = append(del.Quarters, r.Quarter)
		}
		users, entries, cursors := ledgerParams(batch)
		if err := writeLedger(ctx, q, users, entries, cursors); err != nil {
			return err
		}
		if err := q.DeleteEgress(ctx, del); err != nil {
			return fmt.Errorf("delete priced egress: %w", err)
		}
		return nil
	})
	if err != nil {
		return 0, fmt.Errorf("meter egress: %w", err)
	}
	return n, nil
}

func orNil(id *uuid.UUID) uuid.UUID {
	if id == nil {
		return uuid.Nil
	}
	return *id
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
