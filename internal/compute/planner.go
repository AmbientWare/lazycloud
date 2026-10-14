package compute

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"slices"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/cpu"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// demandBatch bounds the pending containers one pass considers.
	demandBatch = 2000
	// planExpiry is how long a published plan stays current; an expired
	// plan is no plan.
	planExpiry = 5 * time.Minute
	// planRefresh is how often a pass that acted on nothing publishes the
	// plan again.
	planRefresh = time.Minute
	// preparingLimit bounds a return to the reserve whose agent never
	// answers. The session asks again every ReserveAttemptTimeout, and an
	// agent update in flight is waited out.
	preparingLimit = 15 * time.Minute
	// ownerPlatform keys the platform's cooldowns; a connection's are keyed
	// by its id.
	ownerPlatform = string(KindPlatform)
)

// CapacityWait says why a pending container waits for compute, or why a
// market falls short.
type CapacityWait string

const (
	// WaitNone means nothing falls short.
	WaitNone CapacityWait = ""
	// WaitProvisioning means a host being bought or resumed will take it.
	WaitProvisioning CapacityWait = "provisioning"
	// WaitLimit means the fleet limit holds the purchase back.
	WaitLimit CapacityWait = "limit"
	// WaitBatch means purchases wait for arrivals to settle.
	WaitBatch CapacityWait = "batch"
	// WaitDeferred means the market's growth continues next pass.
	WaitDeferred CapacityWait = "deferred"
	// WaitDemand means a market's headroom and reserve purchases wait while
	// its work does: warm slots take only room bought for the work.
	WaitDemand CapacityWait = "demand"
	// WaitReturning means reserve purchases wait while hosts that may return
	// to the reserve cover what it lacks.
	WaitReturning CapacityWait = "returning"
	// WaitCooldown means the offers that could host it cool after refusals.
	WaitCooldown CapacityWait = "cooldown"
	// WaitQuota means the vCPU quotas leave no room for an offer that could.
	WaitQuota CapacityWait = "quota"
	// WaitMargin means no offer that could host it keeps the purchase margin.
	WaitMargin CapacityWait = "margin"
	// WaitNoOffer means no offer can host it.
	WaitNoOffer CapacityWait = "no_offer"
)

// Message is the sentence a start failure or the fleet page names for the
// wait.
func (w CapacityWait) Message() string {
	switch w {
	case WaitNone:
		return ""
	case WaitProvisioning:
		return "a host is being bought for it"
	case WaitLimit:
		return "fleet host limit reached"
	case WaitBatch:
		return "purchases wait for arrivals to settle"
	case WaitDeferred:
		return "growth continues next pass"
	case WaitDemand:
		return "headroom waits for pending work"
	case WaitReturning:
		return "reserves wait for hosts to return"
	case WaitCooldown:
		return "offers cooling after refusals"
	case WaitQuota:
		return "vCPU quota reached"
	case WaitMargin:
		return "no offer keeps the purchase margin"
	case WaitNoOffer:
	}
	return "no offer fits"
}

// PlanResult summarizes one planning pass.
type PlanResult struct {
	// Skipped means another planner held the capacity lock.
	Skipped bool
	// Requested counts hosts inserted for launch, Resumed reserves asked
	// to start, Returned hosts sent to prepare for the reserve, Drained
	// hosts draining, Retired reserves terminating or removed and Failed
	// reserves whose agent never answered.
	Requested, Resumed, Returned, Drained, Retired, Failed int
	// Limited counts containers the fleet limit holds back.
	Limited int
	// BatchWait is how long the purchases the pass held wait for arrivals
	// to settle; the planner runs again once it passes.
	BatchWait time.Duration
}

// Plan is the fleet planning pass. Under the capacity lock and in one
// transaction it reads one snapshot of hosts, pending demand, cooldowns,
// prices, quotas and the published markets; decides with PlanFleet for the
// platform and for each connected account; and writes the intents the
// launcher, the reserve actuator and host sessions carry out, the container
// waits and the published plan. EC2 calls run outside it.
func (c *Compute) Plan(ctx context.Context, logger *slog.Logger) (PlanResult, error) {
	// After the scheduler was down past their age, Spot prices are refreshed
	// before the pass prices a purchase from them, at most once a refresh
	// interval while EC2 fails to answer.
	stale, err := c.queries.SpotPricesStale(ctx, spotPriceFresh.Seconds())
	if err != nil {
		return PlanResult{}, fmt.Errorf("read spot price freshness: %w", err)
	}
	if last := c.staleRefresh.Load(); stale && time.Since(time.Unix(0, last)) >= SpotPriceInterval && c.staleRefresh.CompareAndSwap(last, time.Now().UnixNano()) {
		if _, _, failed, err := c.refreshPrices(ctx, logger); err != nil || failed != nil {
			logger.WarnContext(ctx, "spot prices stale for planning", "error", errors.Join(err, failed))
		}
	}
	var pass *fleetPass
	err = pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		pass = nil
		q := c.queries.WithTx(tx)
		lock, err := q.LockFleet(ctx)
		if err != nil {
			return fmt.Errorf("lock fleet: %w", err)
		}
		if !lock.Locked {
			return nil
		}
		p := c.policy()
		read, err := readFleet(ctx, q, p, lock.Now, c.fleet.CapacityCooldown)
		if err != nil {
			return err
		}
		pass = newFleetPass(c, p, read)
		if err := pass.decide(); err != nil {
			return err
		}
		return pass.write(ctx, tx, q)
	})
	if err != nil {
		return PlanResult{}, fmt.Errorf("plan fleet: %w", err)
	}
	if pass == nil {
		return PlanResult{Skipped: true}, nil
	}
	pass.log(ctx, logger)
	pass.trace(ctx)
	return pass.result, nil
}

// trace records each decision in its container's trace.
func (ps *fleetPass) trace(ctx context.Context) {
	for _, d := range ps.decisions {
		_, span := telemetry.StartFor(ctx, d.traceparent, "compute.capacity", trace.WithAttributes(telemetry.Container(d.container.String()), attribute.String("lazycloud.wait", d.wait.wait)))
		if d.wait.host != uuid.Nil {
			span.SetAttributes(telemetry.Host(d.wait.host.String()))
		}
		span.End()
	}
}

// policy is the platform fleet policy.
func (c *Compute) policy() Policy {
	return DefaultPolicy()
}

// connectionPolicy is a connected account's: no headroom and no reserves,
// so an idle host leaves after the idle timeout.
func connectionPolicy(p Policy) Policy {
	p.Spot, p.OnDemand, p.GPU = MarketReserve{}, MarketReserve{}, nil
	return p
}

// fleetPass is one planning pass: its snapshot, its decisions and the
// writes they make.
type fleetPass struct {
	c     *Compute
	p     Policy
	r     fleetRead
	rows  map[HostID]PlannerHostsRow
	waits map[uuid.UUID]waitRow
	// traces holds the pending containers' traces and earlier waits.
	traces map[uuid.UUID]pendingTrace
	w      fleetWrites
	// reasons are why the pass acts on each host it names or buys.
	reasons map[uuid.UUID]ActionReason
	result  PlanResult
	// notes are what the pass logs once it commits.
	notes []note
	// decisions are the hosts the pass newly gave waiting containers, which
	// it traces once it commits.
	decisions []capacityDecision
}

// capacityDecision is a host a pass bought or resumed for a container, or
// the fleet limit it found, in the container's trace.
type capacityDecision struct {
	traceparent string
	container   uuid.UUID
	wait        waitRow
}

type note struct {
	msg   string
	attrs []any
}

type waitRow struct {
	wait string
	host uuid.UUID
}

type pendingTrace struct {
	traceparent string
	bought      uuid.UUID
	wait        string
}

// fleetWrites are a pass's intents, each written by one statement.
type fleetWrites struct {
	stuck, drains, retirePreparing, retires, returns, hibernate []uuid.UUID
	handBacks, lends                                            []uuid.UUID
	buys                                                        []requestedHost
	resumes                                                     []uuid.UUID
	idle                                                        []idleRow
	cools                                                       []coolRow
	markets                                                     []marketRow
}

// requestedHost is a host to buy as InsertRequestedHosts takes it.
type requestedHost struct {
	ID                 uuid.UUID    `json:"id"`
	Kind               HostKind     `json:"kind"`
	ConnectionID       *uuid.UUID   `json:"connection_id"`
	CPUMillis          cpu.Millis   `json:"cpu_millis"`
	MemoryBytes        int64        `json:"memory_bytes"`
	GPUType            string       `json:"gpu_type"`
	GPUCount           int          `json:"gpu_count"`
	Region             string       `json:"region"`
	AvailabilityZone   string       `json:"availability_zone"`
	AvailabilityZoneID string       `json:"availability_zone_id"`
	InstanceType       string       `json:"instance_type"`
	Market             Market       `json:"market"`
	HourlyMicros       int64        `json:"hourly_micros"`
	ReserveMode        *ReserveMode `json:"reserve_mode"`
	Replaces           *uuid.UUID   `json:"replaces"`
	HoldsCPUMillis     *cpu.Millis  `json:"holds_cpu_millis"`
	HoldsMemoryBytes   *int64       `json:"holds_memory_bytes"`
	HoldsDisks         *int         `json:"holds_disks"`
	// MarginWaived is set on a purchase made at a loss, which its launch
	// walk keeps.
	MarginWaived bool `json:"margin_waived"`
}

type idleRow struct {
	ID        uuid.UUID  `json:"id"`
	IdleSince *time.Time `json:"idle_since"`
	FreeSince *time.Time `json:"free_since"`
}

// coolRow is an offer a pass cools in every zone of its region, and why.
type coolRow struct {
	connectionKey, region, instanceType, reason string
	market                                      Market
}

func newFleetPass(c *Compute, p Policy, r fleetRead) *fleetPass {
	ps := &fleetPass{c: c, p: p, r: r, rows: map[HostID]PlannerHostsRow{}, waits: map[uuid.UUID]waitRow{}, reasons: map[uuid.UUID]ActionReason{}}
	for _, h := range r.hosts {
		ps.rows[HostID(h.ID)] = h
	}
	for _, g := range r.pending {
		for _, id := range g.Ids {
			ps.waits[id] = waitRow{}
		}
	}
	return ps
}

// decide plans the platform fleet, then each connected account.
func (ps *fleetPass) decide() error {
	groups, err := pendingGroups(ps.r.pending)
	if err != nil {
		return err
	}
	ps.traces = map[uuid.UUID]pendingTrace{}
	for _, r := range ps.r.pending {
		for n, id := range r.Ids {
			if n < len(r.Traceparents) && n < len(r.Bought) {
				ps.traces[id] = pendingTrace{traceparent: r.Traceparents[n], bought: r.Bought[n]}
			}
		}
	}
	if err := ps.platform(groups); err != nil {
		return err
	}
	return ps.connections(groups)
}

// platform plans the platform fleet and publishes its plan.
func (ps *fleetPass) platform(groups []pendingGroup) error {
	now := ps.r.now
	var hosts []FleetHost
	// failed are hosts that could not prove a stop into the reserve.
	var failed []PlannerHostsRow
	for _, row := range ps.r.hosts {
		if !ownedBy(row, nil) {
			continue
		}
		if stuckPreparing(row, now) {
			ps.w.stuck = append(ps.w.stuck, row.ID)
			failed = append(failed, row)
			continue
		}
		if refusedReserve(row) && row.Containers == 0 && now.Sub(row.PhaseAt) < ps.c.fleet.CapacityCooldown {
			failed = append(failed, row)
		}
		hosts = append(hosts, fleetHostOf(row, now))
	}
	var pending []DemandGroup
	for _, g := range groups {
		if g.connection == nil {
			pending = append(pending, g.group)
		}
	}
	in := ps.r.inputs(ps.c.fleet.StorageRegion, ps.c.fleet.Networks, nil, hosts)
	// The offer of a host that could not prove a stop cools, so this pass
	// does not buy its replacement from it.
	var unproven []OfferCooldown
	for _, row := range failed {
		unproven = append(unproven, OfferCooldown{
			Region: row.Region, InstanceType: row.InstanceType, Market: marketOf(row.Market), Until: now.Add(ps.c.fleet.CapacityCooldown),
		})
	}
	in.Cooldowns = append(in.Cooldowns, unproven...)
	ps.cool(ownerPlatform, unproven, "its host could not prove a stop into the reserve")
	// A reserve being prepared or stopping runs, so it holds host room as
	// well as reserve room.
	held, reserves := 0, 0
	for _, h := range hosts {
		if h.reserve() {
			reserves++
		}
		if !h.reserve() || h.State == FleetPreparing || h.State == FleetStopping {
			held++
		}
	}
	recent, builds, arrived, disks := shapesByMarket(ps.r.recent)
	s := FleetSnapshot{
		Now: now, Hosts: hosts, Pending: pending, Recent: recent, RecentDisks: disks, Builds: builds, Arrived: arrived, Offers: in,
		HostRoom: max(0, ps.c.fleet.MaxHosts-held), ReserveRoom: max(0, ps.c.fleet.MaxHosts-reserves), BatchWait: ps.r.batchWait,
	}
	s.FloorShortSince, s.Peaks = ps.carried()
	plan, cools := planOwner(ps.p, s, ps.c.fleet.CapacityCooldown)
	ps.cool(ownerPlatform, cools, "the host bought for a container could not take it")
	bought, err := ps.apply(plan, nil)
	if err != nil {
		return err
	}
	ps.settleWaits(plan, bought)
	ps.settleIdle(hosts, plan)
	ps.result.BatchWait = max(ps.result.BatchWait, plan.BatchWait)
	return ps.publish(plan)
}

// connections plans each connected account that takes workloads: its
// pending containers on its hosts or new ones within its own networks, and
// its idle hosts' departure. Its accounts pay for their hosts, so no margin
// applies, and they keep no reserves.
func (ps *fleetPass) connections(groups []pendingGroup) error {
	p := connectionPolicy(ps.p)
	for _, conn := range ps.r.connections {
		var hosts []FleetHost
		for _, row := range ps.r.hosts {
			if ownedBy(row, &conn.ID) {
				hosts = append(hosts, fleetHostOf(row, ps.r.now))
			}
		}
		var pending []DemandGroup
		for _, g := range groups {
			if g.connection != nil && *g.connection == conn.ID {
				pending = append(pending, g.group)
			}
		}
		if len(hosts) == 0 && len(pending) == 0 {
			continue
		}
		var networks map[string]Network
		if err := json.Unmarshal(conn.Networks, &networks); err != nil {
			return fmt.Errorf("decode networks of connection %s: %w", conn.ID, err)
		}
		in := ps.r.inputs(ps.c.fleet.StorageRegion, networks, &conn.ID, hosts)
		held := 0
		for _, h := range hosts {
			if !h.reserve() {
				held++
			}
		}
		s := FleetSnapshot{Now: ps.r.now, Hosts: hosts, Pending: pending, Offers: in, HostRoom: max(0, ps.c.fleet.MaxHosts-held), BatchWait: ps.r.connectionWaits[conn.ID]}
		plan, cools := planOwner(p, s, ps.c.fleet.CapacityCooldown)
		ps.cool(conn.ID.String(), cools, "the host bought for a container could not take it")
		bought, err := ps.apply(plan, &conn.ID)
		if err != nil {
			return err
		}
		ps.settleWaits(plan, bought)
		ps.settleIdle(hosts, plan)
		ps.result.BatchWait = max(ps.result.BatchWait, plan.BatchWait)
	}
	return nil
}

// planOwner runs PlanFleet with the offers cooled whose bought host serves
// and is too small ever to take the container bought for it: the offer did
// not hold what it predicted, so buying it again would not help.
func planOwner(p Policy, s FleetSnapshot, cooldown time.Duration) (FleetPlan, []OfferCooldown) {
	var cools []OfferCooldown
	for _, g := range s.Pending {
		for _, c := range g.Containers {
			i := slices.IndexFunc(s.Hosts, func(h FleetHost) bool { return c.Host != nil && h.ID == *c.Host && h.State == FleetServing })
			if i < 0 {
				continue
			}
			h := s.Hosts[i]
			missed := OfferCooldown{Region: h.Region, InstanceType: h.InstanceType, Market: h.Market, Until: s.Now.Add(cooldown)}
			if !h.Usable.Covers(reservedShape(g.Need)) && !slices.ContainsFunc(cools, func(o OfferCooldown) bool { return o == missed }) {
				cools = append(cools, missed)
			}
		}
	}
	s.Offers.Cooldowns = append(slices.Clone(s.Offers.Cooldowns), cools...)
	return PlanFleet(p, s), cools
}

// cool writes the pass's own cooldowns.
func (ps *fleetPass) cool(owner string, cools []OfferCooldown, why string) {
	for _, c := range cools {
		ps.w.cools = append(ps.w.cools, coolRow{connectionKey: owner, region: c.Region, instanceType: c.InstanceType, market: c.Market, reason: why})
		ps.note("offer cooled", "reason", why, "owner", owner, "region", c.Region, "instance_type", c.InstanceType, "market", c.Market)
	}
}

// apply turns one owner's plan into writes. It returns, by action index,
// the host each resume names or each purchase buys.
func (ps *fleetPass) apply(plan FleetPlan, connection *uuid.UUID) (map[int]uuid.UUID, error) {
	hosts := map[int]uuid.UUID{}
	for i, a := range plan.Actions {
		if a.Host != nil {
			ps.reasons[uuid.UUID(*a.Host)] = a.Reason
		}
		switch a.Kind {
		case ActionBuy, ActionBuyReserve, ActionRightsize:
			id, err := uuid.NewV7()
			if err != nil {
				return nil, fmt.Errorf("name a host: %w", err)
			}
			ps.w.buys = append(ps.w.buys, requested(id, connection, *a.Offer, a))
			ps.reasons[id] = a.Reason
			hosts[i] = id
		case ActionResume:
			ps.w.resumes = append(ps.w.resumes, uuid.UUID(*a.Host))
			hosts[i] = uuid.UUID(*a.Host)
		case ActionReturnToReserve:
			ps.w.returns = append(ps.w.returns, uuid.UUID(*a.Host))
			if *a.Mode == ReserveHibernate {
				ps.w.hibernate = append(ps.w.hibernate, uuid.UUID(*a.Host))
			}
		case ActionDrain:
			ps.w.drains = append(ps.w.drains, uuid.UUID(*a.Host))
		case ActionHandBack:
			ps.w.handBacks = append(ps.w.handBacks, uuid.UUID(*a.Host))
		case ActionLend:
			ps.w.lends = append(ps.w.lends, uuid.UUID(*a.Host))
		case ActionCancel:
			ps.w.retires = append(ps.w.retires, uuid.UUID(*a.Host))
		case ActionRetireReserve:
			switch Phase(ps.rows[*a.Host].Phase) {
			case PhaseStopped, PhaseRequested:
				ps.w.retires = append(ps.w.retires, uuid.UUID(*a.Host))
			case PhasePreparing:
				ps.w.retirePreparing = append(ps.w.retirePreparing, uuid.UUID(*a.Host))
			case PhaseProvisioning, PhaseBooting, PhaseJoining, PhaseReady, PhaseDraining, PhaseStopping, PhaseResuming,
				PhaseTerminating, PhaseDeleted, PhaseFailed:
				// It is launching or moving; the next pass decides again.
			}
		}
	}
	return hosts, nil
}

// requested is the host a purchase inserts: the offer's usable capacity,
// as the launcher and placement expect until the host reports its own.
func requested(id uuid.UUID, connection *uuid.UUID, o FleetOffer, a FleetAction) requestedHost {
	h := requestedHost{
		ID: id, Kind: KindPlatform, ConnectionID: connection, CPUMillis: o.Usable.CPUMillis, MemoryBytes: o.Usable.MemoryBytes,
		GPUType: o.Type.GPU, GPUCount: o.Type.GPUCount, Region: o.Region, AvailabilityZone: o.Zone, AvailabilityZoneID: o.ZoneID,
		InstanceType: o.Type.Name, Market: o.Market, HourlyMicros: o.HourlyMicros, MarginWaived: a.Reason == ReasonSpotWait,
	}
	if !a.Holds.Empty() {
		h.HoldsCPUMillis, h.HoldsMemoryBytes = &a.Holds.CPUMillis, &a.Holds.MemoryBytes
	}
	if connection != nil {
		h.Kind = KindConnection
	}
	switch a.Kind {
	case ActionBuyReserve:
		h.ReserveMode, h.HoldsDisks = a.Mode, ptr(o.Type.DiskSlots())
	case ActionRightsize:
		h.Replaces = ptr(uuid.UUID(*a.Host))
	case ActionBuy, ActionResume, ActionReturnToReserve, ActionDrain, ActionRetireReserve, ActionCancel, ActionHandBack, ActionLend:
	}
	return h
}

// settleWaits records why each planned container waits: the host it waits
// for, or the fleet limit.
func (ps *fleetPass) settleWaits(plan FleetPlan, bought map[int]uuid.UUID) {
	for _, w := range plan.Waits {
		row := waitRow{}
		if w.Wait != nil {
			row.wait = string(*w.Wait)
		}
		switch {
		case w.Host != nil:
			row.host = uuid.UUID(*w.Host)
		case w.Action != nil:
			id, ok := bought[*w.Action]
			if !ok {
				row = waitRow{}
			}
			row.host = id
		}
		if row.wait == string(WaitLimit) {
			ps.result.Limited++
		}
		if t, ok := ps.traces[w.Container]; ok && t.traceparent != "" && (row.host != t.bought || row.wait != t.wait) && row.wait != "" {
			ps.decisions = append(ps.decisions, capacityDecision{traceparent: t.traceparent, container: w.Container, wait: row})
		}
		ps.waits[w.Container] = row
	}
}

// settleIdle writes when each serving host became idle and free where
// either changed.
func (ps *fleetPass) settleIdle(hosts []FleetHost, plan FleetPlan) {
	of := func(m map[HostID]time.Time, id HostID) *time.Time {
		if since, ok := m[id]; ok {
			return &since
		}
		return nil
	}
	for _, h := range hosts {
		if h.State != FleetServing {
			continue
		}
		idle, free := of(plan.IdleSince, h.ID), of(plan.FreeSince, h.ID)
		if !sameTime(idle, h.IdleSince) || !sameTime(free, h.FreeSince) {
			ps.w.idle = append(ps.w.idle, idleRow{ID: uuid.UUID(h.ID), IdleSince: idle, FreeSince: free})
		}
	}
}

// carried is what each market's published plan carries to the next pass:
// when its stopped target went short, and its load peak. An unreadable
// plan reads as never short and without a peak.
func (ps *fleetPass) carried() (map[ReserveMarket]time.Time, map[ReserveMarket]LoadPeak) {
	since, peaks := map[ReserveMarket]time.Time{}, map[ReserveMarket]LoadPeak{}
	for _, m := range ps.r.markets {
		var stored PublishedMarket
		if json.Unmarshal(m.Plan, &stored) != nil {
			continue
		}
		market := ReserveMarket{Preemptible: stored.Preemptible, GPU: stored.GPUType}
		if stored.FloorShortSince != nil {
			since[market] = *stored.FloorShortSince
		}
		peaks[market] = stored.Peak
	}
	return since, peaks
}

// publish writes every platform market's plan when the pass acted, a
// market's reserve shortfall began or ended, its load peak moved, or the
// last plan is planRefresh old, and logs each decision that changed.
func (ps *fleetPass) publish(plan FleetPlan) error {
	var last time.Time
	for _, m := range ps.r.markets {
		if m.GeneratedAt.After(last) {
			last = m.GeneratedAt
		}
	}
	stored, peaks := ps.carried()
	moved := slices.ContainsFunc(plan.Markets, func(mp MarketPlan) bool {
		since, ok := stored[mp.Market]
		peak := peaks[mp.Market]
		return ok != (mp.FloorShortSince != nil) || ok && !since.Equal(*mp.FloorShortSince) ||
			peak.Load != mp.Peak.Load || !peak.At.Equal(mp.Peak.At)
	})
	if len(plan.Actions) == 0 && !moved && ps.r.now.Sub(last) < planRefresh {
		return nil
	}
	for _, mp := range plan.Markets {
		key := mp.Market.String()
		actions := slices.DeleteFunc(slices.Clone(plan.Actions), func(a FleetAction) bool { return a.Market != mp.Market })
		published := publishedMarket(mp, actions)
		raw, err := json.Marshal(published)
		if err != nil {
			return fmt.Errorf("encode the plan of market %s: %w", key, err)
		}
		var before struct {
			Decision string `json:"decision"`
		}
		if stored, ok := ps.r.markets[key]; ok {
			_ = json.Unmarshal(stored.Plan, &before) //nolint:errcheck // An unreadable old plan logs the new decision.
		}
		if before.Decision != published.Decision {
			ps.note("fleet market plan", "market", key, "decision", published.Decision)
		}
		ps.w.markets = append(ps.w.markets, marketRow{Market: key, Plan: raw, GeneratedAt: ps.r.now, ExpiresAt: ps.r.now.Add(planExpiry)})
	}
	return nil
}

func sameTime(a, b *time.Time) bool {
	return (a == nil) == (b == nil) && (a == nil || a.Equal(*b))
}

func (ps *fleetPass) note(msg string, attrs ...any) {
	ps.notes = append(ps.notes, note{msg: msg, attrs: attrs})
}

// write applies the pass's intents in its transaction, one statement each,
// and wakes the fleet loops and the sessions of moved hosts once it
// commits. Each move is guarded by the phase the snapshot saw.
func (ps *fleetPass) write(ctx context.Context, tx pgx.Tx, q *Queries) error {
	for _, t := range [][2]Phase{
		{PhasePreparing, PhaseTerminating}, {PhaseStopped, PhaseResuming}, {PhaseStopping, PhaseResuming}, {PhaseReady, PhasePreparing},
		{PhaseReady, PhaseDraining}, {PhasePreparing, PhaseReady}, {PhasePreparing, PhaseDraining}, {PhaseStopped, PhaseTerminating},
		{PhaseRequested, PhaseDeleted},
	} {
		if err := transition(t[0], t[1]); err != nil {
			return err
		}
	}
	var wake []string
	moved := false
	if len(ps.w.stuck) > 0 {
		failed, err := q.FailPreparing(ctx, FailPreparingParams{
			Ids: ps.w.stuck, Seconds: preparingLimit.Seconds(),
			Message: fmt.Sprintf("The agent did not prove the host could stop within %s", preparingLimit),
		})
		if err != nil {
			return fmt.Errorf("fail unproven reserves: %w", err)
		}
		for _, id := range failed {
			if _, err := ps.c.containers.StopHostContainers(ctx, tx, HostID(id), "the host failed to stop into the reserve"); err != nil {
				return err
			}
			ps.note("reserve failed: its agent never proved it could stop", "host_id", id)
		}
		ps.result.Failed, moved = len(failed), moved || len(failed) > 0
	}
	if len(ps.w.buys) > 0 {
		raw, err := json.Marshal(ps.w.buys)
		if err != nil {
			return fmt.Errorf("encode requested hosts: %w", err)
		}
		if err := q.InsertRequestedHosts(ctx, raw); err != nil {
			return fmt.Errorf("insert requested hosts: %w", err)
		}
		for _, h := range ps.w.buys {
			ps.note("host requested", "host_id", h.ID, "instance_type", h.InstanceType, "market", h.Market, "region", h.Region,
				"zone_id", h.AvailabilityZoneID, "reserve_mode", h.ReserveMode, "reason", ps.reasons[h.ID].Message())
		}
		ps.result.Requested, moved = len(ps.w.buys), true
	}
	if len(ps.w.resumes) > 0 {
		resumed, err := q.ResumeReserves(ctx, ps.w.resumes)
		if err != nil {
			return fmt.Errorf("resume reserves: %w", err)
		}
		ps.result.Resumed, moved = len(resumed), moved || len(resumed) > 0
		for _, id := range resumed {
			ps.note("reserve resuming", "host_id", id, "reason", ps.reasons[id].Message())
		}
	}
	if len(ps.w.returns) > 0 {
		returned, err := q.ReturnToReserve(ctx, ReturnToReserveParams{Ids: ps.w.returns, HibernateIds: ps.w.hibernate})
		if err != nil {
			return fmt.Errorf("return hosts to the reserve: %w", err)
		}
		for _, id := range returned {
			wake = append(wake, id.String())
			ps.note("host returning to the reserve", "host_id", id, "reason", ps.reasons[id].Message())
		}
		if err := ps.evictPacked(ctx, tx, returned); err != nil {
			return err
		}
		ps.result.Returned, moved = len(returned), moved || len(returned) > 0
	}
	for _, d := range []struct {
		ids    []uuid.UUID
		reason string
	}{{ps.w.drains, ReasonIdle.Message()}, {ps.w.retirePreparing, ReasonSurplus.Message()}} {
		if len(d.ids) == 0 {
			continue
		}
		drained, err := q.DrainHosts(ctx, DrainHostsParams{Ids: d.ids, Reason: d.reason})
		if err != nil {
			return fmt.Errorf("drain hosts: %w", err)
		}
		for _, id := range drained {
			ps.note("host draining", "host_id", id, "reason", d.reason)
		}
		if err := ps.evictPacked(ctx, tx, drained); err != nil {
			return err
		}
		ps.result.Drained += len(drained)
		moved = moved || len(drained) > 0
	}
	if len(ps.w.handBacks)+len(ps.w.lends) > 0 {
		changed, err := q.SetHandsBack(ctx, SetHandsBackParams{BackIds: ps.w.handBacks, LendIds: ps.w.lends})
		if err != nil {
			return fmt.Errorf("set hosts handing work back: %w", err)
		}
		for _, id := range changed {
			msg := "host lending to Spot-tolerant work"
			if slices.Contains(ps.w.handBacks, id) {
				msg = "host handing Spot-tolerant work back"
			}
			ps.note(msg, "host_id", id, "reason", ps.reasons[id].Message())
		}
		if err := ps.c.containers.WakeReleasesOnHosts(ctx, tx, changed); err != nil {
			return err
		}
		moved = moved || len(changed) > 0
	}
	if len(ps.w.retires) > 0 {
		retired, err := q.RetireReserves(ctx, ps.w.retires)
		if err != nil {
			return fmt.Errorf("retire reserves: %w", err)
		}
		for _, id := range retired {
			ps.note("reserve retiring", "host_id", id, "reason", ps.reasons[id].Message())
		}
		ps.result.Retired, moved = len(retired), moved || len(retired) > 0
	}
	if len(ps.w.idle) > 0 {
		raw, err := json.Marshal(ps.w.idle)
		if err != nil {
			return fmt.Errorf("encode idle hosts: %w", err)
		}
		if err := q.SetIdleSince(ctx, raw); err != nil {
			return fmt.Errorf("record idle hosts: %w", err)
		}
	}
	if err := ps.writeWaits(ctx, q); err != nil {
		return err
	}
	for _, c := range ps.w.cools {
		if err := q.InsertCooldown(ctx, InsertCooldownParams{
			ConnectionKey: c.connectionKey, Region: c.region, InstanceType: c.instanceType, Market: string(c.market),
			Seconds: ps.c.fleet.CapacityCooldown.Seconds(), MaxSeconds: (cooldownGrowth * ps.c.fleet.CapacityCooldown).Seconds(), Reason: c.reason,
		}); err != nil {
			return fmt.Errorf("cool offer: %w", err)
		}
	}
	if len(ps.w.markets) > 0 {
		raw, err := json.Marshal(ps.w.markets)
		if err != nil {
			return fmt.Errorf("encode fleet markets: %w", err)
		}
		if err := q.UpsertFleetMarkets(ctx, raw); err != nil {
			return fmt.Errorf("publish fleet markets: %w", err)
		}
	}
	if len(wake) > 0 {
		if err := q.NotifyHosts(ctx, NotifyHostsParams{Channel: string(database.ChannelHost), Ids: wake}); err != nil {
			return fmt.Errorf("wake host sessions: %w", err)
		}
	}
	if moved {
		return database.Notify(ctx, tx, ChannelCompute, string(KindPlatform))
	}
	return nil
}

// evictPacked evicts the CPU work packed on GPU hosts that leave, which
// never keeps one up.
func (ps *fleetPass) evictPacked(ctx context.Context, tx pgx.Tx, hosts []uuid.UUID) error {
	var ids []uuid.UUID
	for _, id := range hosts {
		ids = append(ids, ps.rows[HostID(id)].PackedIds...)
	}
	if len(ids) == 0 {
		return nil
	}
	return ps.c.containers.EvictContainers(ctx, tx, ids)
}

// writeWaits records every batch container's wait in one statement, in id
// order.
func (ps *fleetPass) writeWaits(ctx context.Context, q *Queries) error {
	if len(ps.waits) == 0 {
		return nil
	}
	ids := slices.SortedFunc(maps.Keys(ps.waits), func(a, b uuid.UUID) int { return cmp.Compare(a.String(), b.String()) })
	waits, hosts := make([]string, len(ids)), make([]uuid.UUID, len(ids))
	for n, id := range ids {
		waits[n], hosts[n] = ps.waits[id].wait, ps.waits[id].host
	}
	if _, err := q.SetCapacityWaits(ctx, SetCapacityWaitsParams{Ids: ids, Waits: waits, Hosts: hosts}); err != nil {
		return fmt.Errorf("record capacity waits: %w", err)
	}
	return nil
}

// log reports what the pass changed once it committed.
func (ps *fleetPass) log(ctx context.Context, logger *slog.Logger) {
	for _, n := range ps.notes {
		logger.InfoContext(ctx, n.msg, n.attrs...)
	}
}
