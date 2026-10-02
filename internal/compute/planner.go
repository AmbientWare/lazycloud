package compute

import (
	"cmp"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"maps"
	"slices"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/database"
)

const (
	// demandBatch bounds the pending containers one pass considers.
	demandBatch = 2000
	// planExpiry is how long a published plan stays current; an expired
	// plan is no plan.
	planExpiry = 5 * time.Minute
	// preparingLimit bounds a return to the reserve whose agent never
	// answers. The session asks again every ReserveAttemptTimeout, and an
	// agent update in flight is waited out.
	preparingLimit = 15 * time.Minute
	// reasonConsolidating marks a host cordoned while its work moves onto
	// the rest of its market.
	reasonConsolidating = "consolidating"
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
	// Published means the pass planned reserves and published the plan;
	// other passes act on pending demand only.
	Published bool
	// Requested counts hosts inserted for launch, Resumed reserves asked
	// to start, Returned hosts sent to prepare for the reserve, Drained
	// hosts draining, Retired reserves terminating or removed, Cordoned
	// hosts consolidating and Failed reserves whose agent never answered.
	Requested, Resumed, Returned, Drained, Retired, Cordoned, Failed int
	// Limited counts containers the fleet limit holds back.
	Limited int
}

// Plan is the fleet planning pass. Under the capacity lock and in one
// transaction it reads one snapshot of hosts, pending demand, recent and
// scheduled demand, activation timings, cooldowns, prices, quotas and the
// published markets; decides with PlanFleet for the platform and for each
// connected account; and writes the intents the launcher, the reserve
// actuator and host sessions carry out, the container waits and the
// published plan. Pending demand is acted on every pass. Reserve growth,
// retention, consolidation, rightsizing and refresh run, and the plan is
// published, every PlanInterval, or every EarlyPlanInterval while a
// market's running free room has stayed short for Pressure.
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
	return pass.result, nil
}

// policy is the platform fleet policy with the configured idle timeout.
func (c *Compute) policy() Policy {
	p := DefaultPolicy()
	p.IdleTimeout = c.fleet.IdleTimeout
	return p
}

// connectionPolicy is a connected account's: no headroom and no reserves,
// an idle host leaves after the idle timeout, and nothing consolidates.
func connectionPolicy(p Policy) Policy {
	p.Spot, p.OnDemand, p.GPU = MarketReserve{}, MarketReserve{}, nil
	p.ConsolidationPercent, p.ConsolidationLight = 0, p.IdleTimeout
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
	plain    map[string]bool
	// records are the platform markets' consolidation state.
	records map[ReserveMarket]MarketRecord
	waits   map[uuid.UUID]waitRow
	w       fleetWrites
	result  PlanResult
	// notes are what the pass logs once it commits.
	notes []note
}

type note struct {
	msg   string
	attrs []any
}

type waitRow struct {
	wait string
	host uuid.UUID
}

// fleetWrites are a pass's intents, each written by one statement.
type fleetWrites struct {
	stuck, uncordon, drains, retirePreparing, retires, cordons, returns, hibernate []uuid.UUID
	buys                                                                           []requestedHost
	resumes                                                                        []uuid.UUID
	refresh                                                                        []bool
	light                                                                          []lightRow
	cools                                                                          []coolRow
	markets                                                                        []marketRow
}

// requestedHost is a host to buy as InsertRequestedHosts takes it.
type requestedHost struct {
	ID                 uuid.UUID    `json:"id"`
	Kind               HostKind     `json:"kind"`
	ConnectionID       *uuid.UUID   `json:"connection_id"`
	CPUMillis          int64        `json:"cpu_millis"`
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

type lightRow struct {
	ID         uuid.UUID  `json:"id"`
	LightSince *time.Time `json:"light_since"`
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
		reported: map[string]int64{}, plain: plainStops(r.stats), records: map[ReserveMarket]MarketRecord{},
		waits: map[uuid.UUID]waitRow{},
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

// platform plans the platform fleet. Pending demand is acted on every
// pass; the rest of the plan only when a reserve pass is due, which also
// publishes it.
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
		if Phase(row.Phase) == PhaseReady && row.CapacityReason == reasonConsolidating && row.Containers == 0 {
			ps.w.uncordon = append(ps.w.uncordon, row.ID)
		}
		hosts = append(hosts, fleetHostOf(row, now, ps.r.release, ps.plain))
	}
	var pending []DemandGroup
	for _, g := range groups {
		if g.connection == nil {
			pending = append(pending, g.group)
		}
	}
	in := ps.offerInputs(ps.c.fleet.Networks, ownerPlatform, hosts)
	in.Rates, in.Quotas, in.PlainStop = ps.rates, vcpuQuotas(ps.r.quotas), ps.plain
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
	forecast, locations := forecasts(ps.p, ps.r, hosts, groups)
	ps.consolidations()
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
		Now: now, Hosts: hosts, Pending: pending, Forecasts: forecast, Locations: locations, Offers: in,
		Markets: maps.Clone(ps.records), HostRoom: max(0, ps.c.fleet.MaxHosts-held), ReserveRoom: max(0, ps.c.fleet.MaxHosts-reserves),
	}
	plan, cools := planOwner(ps.p, s, ps.c.fleet.CapacityCooldown)
	ps.cool(ownerPlatform, cools, "offer cooled: its host could not take the container bought for")
	pressure := ps.pressure(plan)
	due := ps.reserveDue(pressure)
	ps.result.Published = due
	chosen, bought, err := ps.apply(plan, nil, func(a FleetAction) bool { return due || demandAction(a) })
	if err != nil {
		return err
	}
	ps.settleWaits(plan, bought)
	ps.settleLight(hosts, plan, chosen)
	return ps.publish(plan, chosen, pressure, due)
}

// demandAction is a resume or purchase for pending containers, which every
// pass acts on.
func demandAction(a FleetAction) bool {
	return (a.Kind == ActionResume || a.Kind == ActionBuy) && len(a.Containers) > 0
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
				hosts = append(hosts, fleetHostOf(row, ps.r.now, nil, nil))
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
		chosen, bought, err := ps.apply(plan, &conn.ID, func(FleetAction) bool { return true })
		if err != nil {
			return err
		}
		ps.settleWaits(plan, bought)
		ps.settleLight(hosts, plan, chosen)
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

// consolidations ends each market's consolidation once its host emptied,
// left service or ran past ConsolidationDeadline, and starts the cooldown.
// A host given up on stays cordoned until what is left finishes.
func (ps *fleetPass) consolidations() {
	for key, row := range ps.r.markets {
		m, ok := parseReserveMarket(key)
		if !ok {
			continue
		}
		rec := MarketRecord{
			ConsolidatingHost: (*HostID)(row.ConsolidatingHost), ConsolidationStarted: row.ConsolidationStartedAt,
			CooldownUntil: row.ConsolidationCooldownUntil,
		}
		if rec.ConsolidatingHost != nil {
			h, ok := ps.rows[*rec.ConsolidatingHost]
			done := !ok || Phase(h.Phase) != PhaseReady || h.CapacityReason != reasonConsolidating || h.Containers == 0
			late := rec.ConsolidationStarted == nil || ps.r.now.Sub(*rec.ConsolidationStarted) >= ps.p.ConsolidationDeadline
			if done || late {
				ps.note("consolidation ended", "market", key, "host_id", *rec.ConsolidatingHost, "gave_up", !done)
				rec = MarketRecord{CooldownUntil: ptr(ps.r.now.Add(ps.p.ConsolidationCooldown))}
			}
		}
		ps.records[m] = rec
	}
}

// parseReserveMarket reads ReserveMarket.String.
func parseReserveMarket(s string) (ReserveMarket, bool) {
	market, gpu, ok := strings.Cut(s, ":")
	if !ok || (market != "spot" && market != "on_demand") || gpu == "" {
		return ReserveMarket{}, false
	}
	m := ReserveMarket{Preemptible: market == "spot"}
	if gpu != "cpu" {
		m.GPU = gpu
	}
	return m, true
}

// pressure is when each market's running free room fell short of its warm
// target, nil while it is not short.
func (ps *fleetPass) pressure(plan FleetPlan) map[string]*time.Time {
	out := map[string]*time.Time{}
	for _, mp := range plan.Markets {
		key := mp.Market.String()
		if mp.WarmFree.Covers(mp.WarmTarget) {
			out[key] = nil
			continue
		}
		out[key] = ptr(ps.r.now)
		if stored, ok := ps.r.markets[key]; ok && stored.PressureSince != nil {
			out[key] = stored.PressureSince
		}
	}
	return out
}

// reserveDue reports whether this pass plans reserves: no plan was
// published yet, the last one is PlanInterval old, or it is
// EarlyPlanInterval old while a market has been short for Pressure.
func (ps *fleetPass) reserveDue(pressure map[string]*time.Time) bool {
	if len(ps.r.markets) == 0 {
		return true
	}
	var last time.Time
	for _, m := range ps.r.markets {
		last = maxTime(last, m.GeneratedAt)
	}
	age := ps.r.now.Sub(last)
	if age >= ps.p.PlanInterval {
		return true
	}
	if age < ps.p.EarlyPlanInterval {
		return false
	}
	for _, since := range pressure {
		if since != nil && ps.r.now.Sub(*since) >= ps.p.Pressure {
			return true
		}
	}
	return false
}

func maxTime(a, b time.Time) time.Time {
	if b.After(a) {
		return b
	}
	return a
}

// apply turns the chosen actions of one owner's plan into writes. It
// returns the chosen actions and, by action index, the host each resume
// names or each purchase buys.
func (ps *fleetPass) apply(plan FleetPlan, connection *uuid.UUID, choose func(FleetAction) bool) ([]FleetAction, map[int]uuid.UUID, error) {
	var chosen []FleetAction
	hosts := map[int]uuid.UUID{}
	for i, a := range plan.Actions {
		if !choose(a) {
			continue
		}
		chosen = append(chosen, a)
		switch a.Kind {
		case ActionBuy, ActionBuyReserve, ActionRightsize:
			id, err := uuid.NewV7()
			if err != nil {
				return nil, nil, fmt.Errorf("name a host: %w", err)
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
		case ActionConsolidate:
			ps.w.cordons = append(ps.w.cordons, uuid.UUID(*a.Host))
			ps.records[a.Market] = MarketRecord{ConsolidatingHost: a.Host, ConsolidationStarted: ptr(ps.r.now)}
		}
	}
	return chosen, hosts, nil
}

// size orders requirements largest first, as PlanFleet places them: GPUs
// dominate, then CPU and memory in comparable units.
func size(r Requirement) int {
	return r.GPUsNeeded()<<40 + int(r.CPUMillis) + int(r.MemoryBytes>>20)/4
}

// fitFirst reserves r on the first host that fits it and returns its index,
// or -1.
func fitFirst(hosts []HostCapacity, r Requirement) int {
	for i := range hosts {
		if hosts[i].Fits(r) {
			hosts[i].Reserve(r)
			return i
		}
	}
	return -1
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
		ps.waits[w.Container] = row
	}
}

// settleLight writes when each serving host became lightly used where it
// changed. A host the pass moves clears it in the move; a host still
// consolidating keeps it.
func (ps *fleetPass) settleLight(hosts []FleetHost, plan FleetPlan, chosen []FleetAction) {
	moved := map[HostID]bool{}
	for _, a := range chosen {
		if a.Host != nil && a.Kind != ActionConsolidate {
			moved[*a.Host] = true
		}
	}
	for _, h := range hosts {
		row := ps.rows[h.ID]
		if moved[h.ID] || (row.CapacityReason == reasonConsolidating && row.Containers > 0) {
			continue
		}
		var want *time.Time
		if since, ok := plan.LightSince[h.ID]; ok {
			want = &since
		}
		if (want == nil) != (row.LightSince == nil) || (want != nil && !want.Equal(*row.LightSince)) {
			ps.w.light = append(ps.w.light, lightRow{ID: row.ID, LightSince: want})
		}
	}
}

// publish writes each platform market's row where it changed: its plan on
// a reserve pass, its pressure and consolidation on every pass. A market
// new since the last reserve pass waits for the next one, so the latest
// generated_at stays the last reserve pass.
func (ps *fleetPass) publish(plan FleetPlan, chosen []FleetAction, pressure map[string]*time.Time, due bool) error {
	for _, mp := range plan.Markets {
		key := mp.Market.String()
		stored, has := ps.r.markets[key]
		rec := ps.records[mp.Market]
		row := marketRow{
			Market: key, PressureSince: pressure[key], ConsolidatingHost: (*uuid.UUID)(rec.ConsolidatingHost),
			ConsolidationStartedAt: rec.ConsolidationStarted, ConsolidationCooldownUntil: rec.CooldownUntil,
		}
		if !due && !has {
			continue
		}
		if due {
			growth := slices.DeleteFunc(slices.Clone(chosen), func(a FleetAction) bool { return a.Market != mp.Market })
			published := publishedMarket(mp, growth)
			raw, err := json.Marshal(published)
			if err != nil {
				return fmt.Errorf("encode the plan of market %s: %w", key, err)
			}
			row.Plan, row.GeneratedAt, row.ExpiresAt = raw, ps.r.now, ps.r.now.Add(planExpiry)
			var before struct {
				Decision string `json:"decision"`
			}
			if has {
				_ = json.Unmarshal(stored.Plan, &before) //nolint:errcheck // An unreadable old plan logs the new decision.
			}
			if before.Decision != published.Decision {
				ps.note("fleet market plan", "market", key, "decision", published.Decision)
			}
			ps.w.markets = append(ps.w.markets, row)
			continue
		}
		row.Plan, row.GeneratedAt, row.ExpiresAt = stored.Plan, stored.GeneratedAt, stored.ExpiresAt
		if !sameTime(row.PressureSince, stored.PressureSince) || !sameTime(row.ConsolidationStartedAt, stored.ConsolidationStartedAt) ||
			!sameTime(row.ConsolidationCooldownUntil, stored.ConsolidationCooldownUntil) ||
			(row.ConsolidatingHost == nil) != (stored.ConsolidatingHost == nil) ||
			(row.ConsolidatingHost != nil && *row.ConsolidatingHost != *stored.ConsolidatingHost) {
			ps.w.markets = append(ps.w.markets, row)
		}
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
		{PhasePreparing, PhaseReady}, {PhaseStopped, PhaseTerminating}, {PhaseRequested, PhaseDeleted},
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
	if len(ps.w.uncordon) > 0 {
		if err := q.UncordonHosts(ctx, ps.w.uncordon); err != nil {
			return fmt.Errorf("uncordon consolidated hosts: %w", err)
		}
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
	if len(ps.w.cordons) > 0 {
		cordoned, err := q.CordonHosts(ctx, ps.w.cordons)
		if err != nil {
			return fmt.Errorf("cordon hosts: %w", err)
		}
		for _, id := range cordoned {
			if err := ps.c.containers.DrainHostContainers(ctx, tx, HostID(id)); err != nil {
				return err
			}
			ps.note("consolidating host", "host_id", id)
		}
		ps.result.Cordoned = len(cordoned)
	}
	if len(ps.w.light) > 0 {
		raw, err := json.Marshal(ps.w.light)
		if err != nil {
			return fmt.Errorf("encode light use: %w", err)
		}
		if err := q.SetLightSince(ctx, raw); err != nil {
			return fmt.Errorf("record light use: %w", err)
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
