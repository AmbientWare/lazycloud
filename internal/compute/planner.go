package compute

import (
	"cmp"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"maps"
	"slices"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/billing"
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

// CapacityWait says why a pending container waits for compute.
type CapacityWait string

const (
	// WaitProvisioning means a host being bought or resumed will take it.
	WaitProvisioning CapacityWait = "provisioning"
	// WaitLimit means the fleet limit holds the purchase back.
	WaitLimit CapacityWait = "limit"
)

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
}

// Plan is the fleet planning pass. Under the capacity lock and in one
// transaction it reads one snapshot of hosts, pending demand, cooldowns,
// prices, quotas and the published markets; decides with PlanFleet for the
// platform and for each connected account; and writes the intents the
// launcher, the reserve actuator and host sessions carry out, the container
// waits and the published plan. EC2 calls run outside it.
func (c *Compute) Plan(ctx context.Context, logger *slog.Logger) (PlanResult, error) {
	var pass *fleetPass
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
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
		read, err := readFleet(ctx, q, p, lock.Now)
		if err != nil {
			return err
		}
		if pass, err = newFleetPass(c, p, read); err != nil {
			return err
		}
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

// trace records each decision in its container's trace, linked to the pass.
func (ps *fleetPass) trace(ctx context.Context) {
	pass := telemetry.TraceParentOf(ctx)
	for _, d := range ps.decisions {
		_, span := telemetry.StartIn(ctx, telemetry.TracerOf(ctx), d.traceparent, "compute.capacity", telemetry.LinkTo(pass),
			trace.WithAttributes(telemetry.Container(d.container.String()), attribute.String("lazycloud.wait", d.wait.wait)))
		if d.wait.host != uuid.Nil {
			span.SetAttributes(telemetry.Host(d.wait.host.String()))
		}
		span.End()
	}
}

// policy is the platform fleet policy with the configured idle timeout.
func (c *Compute) policy() Policy {
	p := DefaultPolicy()
	p.IdleTimeout = c.fleet.IdleTimeout
	return p
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
	c        *Compute
	p        Policy
	r        fleetRead
	rows     map[HostID]PlannerHostsRow
	catalog  []CatalogType
	rates    []billing.ComputeRate
	reported map[string]int64
	waits    map[uuid.UUID]waitRow
	// traces holds the pending containers' traces and earlier waits.
	traces map[uuid.UUID]pendingTrace
	w      fleetWrites
	result PlanResult
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
	buys                                                        []requestedHost
	resumes                                                     []uuid.UUID
	refresh                                                     []bool
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
}

type idleRow struct {
	ID        uuid.UUID  `json:"id"`
	IdleSince *time.Time `json:"idle_since"`
}

type coolRow struct {
	ConnectionKey string `json:"connection_key"`
	Region        string `json:"region"`
	InstanceType  string `json:"instance_type"`
	Market        Market `json:"market"`
}

func newFleetPass(c *Compute, p Policy, r fleetRead) (*fleetPass, error) {
	rates, err := billing.FleetComputeRates(r.now)
	if err != nil {
		return nil, fmt.Errorf("read the fleet's compute rates: %w", err)
	}
	ps := &fleetPass{
		c: c, p: p, r: r, rows: map[HostID]PlannerHostsRow{}, catalog: FleetCatalog(), rates: rates,
		reported: map[string]int64{}, waits: map[uuid.UUID]waitRow{},
	}
	for _, h := range r.hosts {
		ps.rows[HostID(h.ID)] = h
		if h.SessionEpoch > 0 && h.InstanceType != "" {
			if seen, ok := ps.reported[h.InstanceType]; !ok || h.MemoryBytes < seen {
				ps.reported[h.InstanceType] = h.MemoryBytes
			}
		}
	}
	for _, g := range r.pending {
		for _, id := range g.Ids {
			ps.waits[id] = waitRow{}
		}
	}
	return ps, nil
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

func (ps *fleetPass) offerInputs(networks map[string]Network, owner string, hosts []FleetHost) OfferInputs {
	zones := map[string]int{}
	for _, h := range hosts {
		if h.State != FleetTerminating && h.ZoneID != "" {
			zones[h.ZoneID]++
		}
	}
	return OfferInputs{
		Now: ps.r.now, Catalog: ps.catalog, Networks: networks, Spot: ps.r.spot, Cooldowns: offerCooldowns(ps.r.cooldowns, owner),
		ReportedMemory: ps.reported, ZoneHosts: zones, ZoneTypes: ps.r.zoneTypes,
	}
}

// platform plans the platform fleet and publishes its plan.
func (ps *fleetPass) platform(groups []pendingGroup) error {
	now := ps.r.now
	var hosts []FleetHost
	// failed are hosts that could not prove a stop into the reserve.
	var failed []PlannerHostsRow
	for _, row := range ps.r.hosts {
		if HostKind(row.Kind) != KindPlatform {
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
		hosts = append(hosts, fleetHostOf(row, now, ps.r.release))
	}
	var pending []DemandGroup
	for _, g := range groups {
		if g.connection == nil {
			pending = append(pending, g.group)
		}
	}
	in := ps.offerInputs(ps.c.fleet.Networks, ownerPlatform, hosts)
	in.Rates, in.Quotas = ps.rates, vcpuQuotas(ps.r.quotas)
	// The offer of a host that could not prove a stop cools, so this pass
	// does not buy its replacement from it.
	var unproven []OfferCooldown
	for _, row := range failed {
		unproven = append(unproven, OfferCooldown{
			Region: row.Region, InstanceType: row.InstanceType, Market: marketOf(row.Market), Until: now.Add(ps.c.fleet.CapacityCooldown),
		})
	}
	in.Cooldowns = append(in.Cooldowns, unproven...)
	ps.cool(ownerPlatform, unproven, "offer cooled: its host could not prove a stop into the reserve")
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
	s := FleetSnapshot{
		Now: now, Hosts: hosts, Pending: pending, Recent: largestShapes(ps.r.recent), Offers: in,
		HostRoom: max(0, ps.c.fleet.MaxHosts-held), ReserveRoom: max(0, ps.c.fleet.MaxHosts-reserves),
		FloorShortSince: ps.floorShortSince(),
	}
	plan, cools := planOwner(ps.p, s, ps.c.fleet.CapacityCooldown)
	ps.cool(ownerPlatform, cools, "offer cooled: its host could not take the container bought for")
	bought, err := ps.apply(plan, nil)
	if err != nil {
		return err
	}
	ps.settleWaits(plan, bought)
	ps.settleIdle(hosts, plan)
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
			if row.ConnectionID != nil && *row.ConnectionID == conn.ID {
				hosts = append(hosts, fleetHostOf(row, ps.r.now, nil))
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
		in := ps.offerInputs(networks, conn.ID.String(), hosts)
		in.OwnerPays = true
		held := 0
		for _, h := range hosts {
			if !h.reserve() {
				held++
			}
		}
		s := FleetSnapshot{Now: ps.r.now, Hosts: hosts, Pending: pending, Offers: in, HostRoom: max(0, ps.c.fleet.MaxHosts-held)}
		plan, cools := planOwner(p, s, ps.c.fleet.CapacityCooldown)
		ps.cool(conn.ID.String(), cools, "offer cooled: its host could not take the container bought for")
		bought, err := ps.apply(plan, &conn.ID)
		if err != nil {
			return err
		}
		ps.settleWaits(plan, bought)
		ps.settleIdle(hosts, plan)
	}
	return nil
}

// planOwner runs PlanFleet, and once more with the offers cooled whose
// bought host joined and still could not take its container: the offer did
// not hold what it predicted, so buying it again would not help.
func planOwner(p Policy, s FleetSnapshot, cooldown time.Duration) (FleetPlan, []OfferCooldown) {
	plan := PlanFleet(p, s)
	bought := map[uuid.UUID]HostID{}
	for _, g := range s.Pending {
		for _, c := range g.Containers {
			if c.Host != nil {
				bought[c.ID] = *c.Host
			}
		}
	}
	var cools []OfferCooldown
	for _, w := range plan.Waits {
		if w.Action == nil && (w.Wait == nil || *w.Wait != WaitLimit) {
			continue
		}
		id, ok := bought[w.Container]
		if !ok {
			continue
		}
		i := slices.IndexFunc(s.Hosts, func(h FleetHost) bool { return h.ID == id && h.State == FleetServing })
		if i < 0 {
			continue
		}
		h := s.Hosts[i]
		c := OfferCooldown{Region: h.Region, InstanceType: h.InstanceType, Market: h.Market, Until: s.Now.Add(cooldown)}
		if !slices.ContainsFunc(cools, func(o OfferCooldown) bool {
			return o.Region == c.Region && o.InstanceType == c.InstanceType && o.Market == c.Market
		}) {
			cools = append(cools, c)
		}
	}
	if len(cools) == 0 {
		return plan, nil
	}
	s.Offers.Cooldowns = append(slices.Clone(s.Offers.Cooldowns), cools...)
	return PlanFleet(p, s), cools
}

// cool writes cooldowns without a refusal time, so they do not count toward
// region cooling.
func (ps *fleetPass) cool(owner string, cools []OfferCooldown, why string) {
	for _, c := range cools {
		ps.w.cools = append(ps.w.cools, coolRow{ConnectionKey: owner, Region: c.Region, InstanceType: c.InstanceType, Market: c.Market})
		ps.note(why, "owner", owner, "region", c.Region, "instance_type", c.InstanceType, "market", c.Market)
	}
}

// apply turns one owner's plan into writes. It returns, by action index,
// the host each resume names or each purchase buys.
func (ps *fleetPass) apply(plan FleetPlan, connection *uuid.UUID) (map[int]uuid.UUID, error) {
	hosts := map[int]uuid.UUID{}
	for i, a := range plan.Actions {
		switch a.Kind {
		case ActionBuy, ActionBuyReserve, ActionRightsize:
			id, err := uuid.NewV7()
			if err != nil {
				return nil, fmt.Errorf("name a host: %w", err)
			}
			ps.w.buys = append(ps.w.buys, requested(id, connection, *a.Offer, a))
			hosts[i] = id
		case ActionResume, ActionRefresh:
			ps.w.resumes = append(ps.w.resumes, uuid.UUID(*a.Host))
			ps.w.refresh = append(ps.w.refresh, a.Kind == ActionRefresh)
			hosts[i] = uuid.UUID(*a.Host)
		case ActionReturnToReserve:
			ps.w.returns = append(ps.w.returns, uuid.UUID(*a.Host))
			if *a.Mode == ReserveHibernate {
				ps.w.hibernate = append(ps.w.hibernate, uuid.UUID(*a.Host))
			}
		case ActionDrain:
			ps.w.drains = append(ps.w.drains, uuid.UUID(*a.Host))
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
		InstanceType: o.Type.Name, Market: o.Market, HourlyMicros: o.HourlyMicros,
	}
	if connection != nil {
		h.Kind = KindConnection
	}
	if a.Kind == ActionBuyReserve {
		h.ReserveMode = a.Mode
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

// settleIdle writes when each serving host became idle where it changed.
func (ps *fleetPass) settleIdle(hosts []FleetHost, plan FleetPlan) {
	for _, h := range hosts {
		if h.State != FleetServing {
			continue
		}
		var want *time.Time
		if since, ok := plan.IdleSince[h.ID]; ok {
			want = &since
		}
		if !sameTime(want, h.IdleSince) {
			ps.w.idle = append(ps.w.idle, idleRow{ID: uuid.UUID(h.ID), IdleSince: want})
		}
	}
}

// floorShortSince is when each market's stopped floor went short, from the
// published plans; an unreadable plan reads as never short.
func (ps *fleetPass) floorShortSince() map[ReserveMarket]time.Time {
	out := map[ReserveMarket]time.Time{}
	for _, m := range ps.r.markets {
		var stored PublishedMarket
		if json.Unmarshal(m.Plan, &stored) != nil || stored.FloorShortSince == nil {
			continue
		}
		out[ReserveMarket{Preemptible: stored.Preemptible, GPU: stored.GPUType}] = *stored.FloorShortSince
	}
	return out
}

// publish writes every platform market's plan when the pass acted, a
// market's floor shortfall began or ended, or the last plan is planRefresh
// old, and logs each decision that changed.
func (ps *fleetPass) publish(plan FleetPlan) error {
	var last time.Time
	for _, m := range ps.r.markets {
		if m.GeneratedAt.After(last) {
			last = m.GeneratedAt
		}
	}
	stored := ps.floorShortSince()
	floorMoved := slices.ContainsFunc(plan.Markets, func(mp MarketPlan) bool {
		since, ok := stored[mp.Market]
		return ok != (mp.FloorShortSince != nil) || ok && !since.Equal(*mp.FloorShortSince)
	})
	if len(plan.Actions) == 0 && !floorMoved && ps.r.now.Sub(last) < planRefresh {
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
		{PhasePreparing, PhaseFailed}, {PhaseStopped, PhaseResuming}, {PhaseReady, PhasePreparing}, {PhaseReady, PhaseDraining},
		{PhasePreparing, PhaseReady}, {PhasePreparing, PhaseDraining}, {PhaseStopped, PhaseTerminating}, {PhaseRequested, PhaseDeleted},
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
				"reserve_mode", h.ReserveMode)
		}
		ps.result.Requested, moved = len(ps.w.buys), true
	}
	if len(ps.w.resumes) > 0 {
		resumed, err := q.ResumeReserves(ctx, ResumeReservesParams{Ids: ps.w.resumes, Refresh: ps.w.refresh})
		if err != nil {
			return fmt.Errorf("resume reserves: %w", err)
		}
		ps.result.Resumed, moved = len(resumed), moved || len(resumed) > 0
		for _, id := range resumed {
			ps.note("reserve resuming", "host_id", id)
		}
	}
	if len(ps.w.returns) > 0 {
		returned, err := q.ReturnToReserve(ctx, ReturnToReserveParams{Ids: ps.w.returns, HibernateIds: ps.w.hibernate})
		if err != nil {
			return fmt.Errorf("return hosts to the reserve: %w", err)
		}
		for _, id := range returned {
			wake = append(wake, id.String())
			ps.note("host returning to the reserve", "host_id", id)
		}
		ps.result.Returned, moved = len(returned), moved || len(returned) > 0
	}
	for _, d := range []struct {
		ids    []uuid.UUID
		reason string
	}{{ps.w.drains, "idle"}, {ps.w.retirePreparing, "the reserve no longer needs it"}} {
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
		ps.result.Drained += len(drained)
		moved = moved || len(drained) > 0
	}
	if len(ps.w.retires) > 0 {
		retired, err := q.RetireReserves(ctx, ps.w.retires)
		if err != nil {
			return fmt.Errorf("retire reserves: %w", err)
		}
		for _, id := range retired {
			ps.note("reserve retiring", "host_id", id)
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
	if len(ps.w.cools) > 0 {
		raw, err := json.Marshal(ps.w.cools)
		if err != nil {
			return fmt.Errorf("encode cooldowns: %w", err)
		}
		if err := q.CoolOffers(ctx, CoolOffersParams{Offers: raw, Seconds: ps.c.fleet.CapacityCooldown.Seconds()}); err != nil {
			return fmt.Errorf("cool offers: %w", err)
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
