package compute

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// fleetRead is what one planning pass reads: everything in its transaction
// but the Spot prices and quota rooms, which their refresh loops write.
type fleetRead struct {
	now time.Time
	// until ends the scheduled forecast: a provision, or the slowest
	// observed activation, and the pass that orders it.
	until     time.Time
	hosts     []PlannerHostsRow
	pending   []PendingDemandRow
	arrivals  []RecentArrivalsRow
	scheduled []ScheduledDemandRow
	stats     []ActivationStat
	cooldowns []PlannerCooldownsRow
	markets   map[string]FleetMarketsRow
	// release is the target agent release; nil when none is published.
	release     *AgentRelease
	connections []HostingConnectionsRow
	spot        []SpotQuote
	quotas      []QuotaRoom
}

// readFleet reads one pass's snapshot with a fixed number of statements,
// each bounded by live rows, the pending batch or a recent window.
func (c *Compute) readFleet(ctx context.Context, q *Queries, p Policy, now time.Time) (fleetRead, error) {
	r := fleetRead{now: now, markets: map[string]FleetMarketsRow{}}
	var err error
	if r.hosts, err = q.PlannerHosts(ctx); err != nil {
		return r, fmt.Errorf("read fleet hosts: %w", err)
	}
	if r.pending, err = q.PendingDemand(ctx, demandBatch); err != nil {
		return r, fmt.Errorf("read pending demand: %w", err)
	}
	if r.arrivals, err = q.RecentArrivals(ctx, uuidFloor(now.Add(-p.History))); err != nil {
		return r, fmt.Errorf("read recent arrivals: %w", err)
	}
	stats, err := q.ActivationStats(ctx)
	if err != nil {
		return r, fmt.Errorf("read activation stats: %w", err)
	}
	horizon := p.TotalHorizon()
	for _, s := range stats {
		stat := ActivationStat{
			Kind: ActivationKind(s.Kind), InstanceType: s.InstanceType, Region: s.Region, GPU: s.GpuType,
			Ready: int(s.Ready), Failed: int(s.Failed),
		}
		if s.ResumeOutcome != "" {
			stat.Outcome = ptr(ResumeOutcome(s.ResumeOutcome))
		}
		if s.Ready > 0 {
			stat.P95 = ptr(time.Duration(s.P95 * float64(time.Second)))
			horizon = max(horizon, *stat.P95+p.PlanInterval)
		}
		r.stats = append(r.stats, stat)
	}
	r.until = now.Add(horizon)
	if r.scheduled, err = q.ScheduledDemand(ctx, ScheduledDemandParams{SinceID: uuidFloor(now.Add(-p.History)), Until: r.until}); err != nil {
		return r, fmt.Errorf("read scheduled demand: %w", err)
	}
	if r.cooldowns, err = q.PlannerCooldowns(ctx, p.RegionFailureWindow.Seconds()); err != nil {
		return r, fmt.Errorf("read cooldowns: %w", err)
	}
	markets, err := q.FleetMarkets(ctx)
	if err != nil {
		return r, fmt.Errorf("read fleet markets: %w", err)
	}
	for _, m := range markets {
		r.markets[m.Market] = m
	}
	switch release, err := q.TargetRelease(ctx); {
	case errors.Is(err, pgx.ErrNoRows):
	case err != nil:
		return r, fmt.Errorf("read target release: %w", err)
	default:
		r.release = ptr(releaseOf(release.Version, release.Sha256Amd64, release.Sha256Arm64, release.RolloutPercent))
	}
	if slices.ContainsFunc(r.pending, func(g PendingDemandRow) bool { return g.ConnectionID != nil }) ||
		slices.ContainsFunc(r.hosts, func(h PlannerHostsRow) bool { return h.ConnectionID != nil }) {
		if r.connections, err = q.HostingConnections(ctx); err != nil {
			return r, fmt.Errorf("read connections: %w", err)
		}
	}
	if r.spot, err = c.SpotPrices(ctx); err != nil {
		return r, err
	}
	if r.quotas, err = c.QuotaRooms(ctx); err != nil {
		return r, err
	}
	return r, nil
}

// uuidFloor is the least uuidv7 stamped at t, so ids created from t on sort
// at or above it.
func uuidFloor(t time.Time) uuid.UUID {
	var stamp [8]byte
	binary.BigEndian.PutUint64(stamp[:], uint64(t.UnixMilli())) //nolint:gosec // Times after 1970.
	var id uuid.UUID
	copy(id[:6], stamp[2:])
	id[6], id[8] = 0x70, 0x80
	return id
}

// fleetStateOf is where a cloud host stands in the fleet. A host bought for
// the reserve or refreshing prepares until it stops; a consolidated host
// that emptied serves again.
func fleetStateOf(h PlannerHostsRow, now time.Time) FleetState {
	reserve := h.ReserveMode != nil
	switch Phase(h.Phase) {
	case PhaseRequested, PhaseProvisioning, PhaseBooting, PhaseJoining, PhaseResuming:
		if reserve {
			return FleetPreparing
		}
		return FleetStarting
	case PhaseReady:
		if CapacityState(h.CapacityState) != CapacityAvailable && (h.CapacityReason != reasonConsolidating || h.Containers > 0) {
			return FleetDraining
		}
		if HostState(h.State) == HostOnline && h.LastSeenAt != nil && now.Sub(*h.LastSeenAt) < LivenessTimeout {
			return FleetServing
		}
		return FleetUnavailable
	case PhaseDraining:
		return FleetDraining
	case PhasePreparing:
		return FleetPreparing
	case PhaseStopping:
		return FleetStopping
	case PhaseStopped:
		if reserve && ReserveMode(*h.ReserveMode) == ReserveHibernate {
			switch ImageEvidence(h.ImageEvidence) {
			case EvidenceSaved:
				return FleetImageSaved
			case EvidenceUnknown:
				return FleetHibernateUnverified
			case EvidenceFailed, EvidenceUnavailable:
			}
		}
		return FleetStopped
	case PhaseTerminating:
		return FleetTerminating
	case PhaseDeleted, PhaseFailed:
		return FleetFailed
	}
	return FleetUnavailable
}

// fleetHostOf is a cloud host as PlanFleet sees it. Only an on-demand host
// or a Spot reserve on a persistent request can stop, and a host of a type
// whose hibernation booted cold in its region stops plainly.
func fleetHostOf(h PlannerHostsRow, now time.Time, release *AgentRelease, plainStop map[string]bool) FleetHost {
	market := marketOf(h.Market)
	return FleetHost{
		ID: HostID(h.ID), InstanceType: h.InstanceType, Region: h.Region, Zone: h.AvailabilityZone, ZoneID: h.AvailabilityZoneID,
		Market: market, GPU: h.GpuType, State: fleetStateOf(h, now),
		Usable:     FleetCapacity{CPUMillis: h.CpuMillis, MemoryBytes: h.MemoryBytes, GPUs: int(h.GpuCount)},
		Load:       FleetCapacity{CPUMillis: h.UsedCpu, MemoryBytes: h.UsedMemory, GPUs: int(h.UsedGpus)},
		Containers: int(h.Containers), Pinned: int(h.Pinned), Protected: h.InterruptionAt != nil, LaunchedAt: h.LaunchedAt,
		Current: preparedFor(h, release), ReserveMode: (*ReserveMode)(h.ReserveMode),
		HibernationConfigured: h.HibernationConfigured && !plainStop[h.Region+"/"+h.InstanceType],
		Stoppable:             market == MarketOnDemand || h.SpotRequestID != nil,
		HourlyMicros:          h.HourlyMicros, LightSince: h.LightSince,
	}
}

// preparedFor reports whether a host last proved the agent release it
// should run: the target once the rollout reaches it, any release outside
// the rollout or without a target.
func preparedFor(h PlannerHostsRow, release *AgentRelease) bool {
	if release == nil || rolloutBucket(HostID(h.ID)) >= release.RolloutPercent {
		return true
	}
	return h.PreparedAgentVersion != nil && *h.PreparedAgentVersion == release.Version
}

// stuckPreparing reports a platform host whose agent has not proved it may
// stop within preparingLimit, outside an agent update.
func stuckPreparing(h PlannerHostsRow, now time.Time) bool {
	return Phase(h.Phase) == PhasePreparing && now.Sub(h.PhaseAt) >= preparingLimit &&
		(h.UpdatingUntil == nil || !h.UpdatingUntil.After(now))
}

// pendingGroup is pending containers with one requirement and owner.
type pendingGroup struct {
	connection *uuid.UUID
	group      DemandGroup
}

// pendingGroups decodes the pending batch. Containers pinned to a joined
// machine wait for it, not for the fleet, and are left out.
func pendingGroups(rows []PendingDemandRow) ([]pendingGroup, error) {
	var out []pendingGroup
	for _, r := range rows {
		if r.Machine != "" {
			continue
		}
		var gpus []string
		if err := json.Unmarshal(r.Gpus, &gpus); err != nil {
			return nil, fmt.Errorf("decode the GPUs pending containers ask for: %w", err)
		}
		g := pendingGroup{connection: r.ConnectionID, group: DemandGroup{Need: Requirement{
			Region: r.Region, Zone: r.Zone, Preemptible: r.Preemptible, GPUs: gpus, GPUCount: int(r.GpuCount),
			CPUMillis: r.CpuMillis, MemoryBytes: r.MemoryBytes,
		}}}
		for n, id := range r.Ids {
			c := PendingContainer{ID: id}
			if r.Bought[n] != uuid.Nil {
				c.Host = ptr(HostID(r.Bought[n]))
			}
			g.group.Containers = append(g.group.Containers, c)
		}
		out = append(out, g)
	}
	return out, nil
}

// reservedShape is what a requirement reserves on a host.
func reservedShape(r Requirement) FleetCapacity {
	return FleetCapacity{CPUMillis: r.CPUMillis, MemoryBytes: r.MemoryBytes, GPUs: r.GPUsNeeded()}
}

// forecastMarket is the market demand of these GPUs is forecast in: CPU
// work by its interruption tolerance, GPU work in the on-demand market of
// the card placement gave it, else of the first reserved card it accepts,
// a card the fleet holds first for "any". GPU work no reserved card serves
// keeps no reserve and is not forecast.
func forecastMarket(p Policy, gpus []string, placed string, preemptible bool, stocked map[string]bool) (ReserveMarket, bool) {
	if len(gpus) == 0 {
		return ReserveMarket{Preemptible: preemptible}, true
	}
	if reservedCard(p, placed) {
		return ReserveMarket{GPU: placed}, true
	}
	for _, model := range gpus {
		if model != GPUAny {
			if reservedCard(p, model) {
				return ReserveMarket{GPU: model}, true
			}
			continue
		}
		var cards []string
		for card := range p.GPU {
			cards = append(cards, card)
		}
		slices.SortFunc(cards, func(a, b string) int {
			if stocked[a] != stocked[b] {
				return boolOrder(!stocked[a], !stocked[b])
			}
			return strings.Compare(a, b)
		})
		if len(cards) > 0 {
			return ReserveMarket{GPU: cards[0]}, true
		}
	}
	return ReserveMarket{}, false
}

// placementKey is demand pinned to a region or zone, in one market and
// shape.
type placementKey struct {
	market       ReserveMarket
	region, zone string
	shape        FleetCapacity
}

// demandInputs are one market's forecast inputs.
type demandInputs struct {
	arrivals  []Arrival
	workloads []ScheduledWorkload
	pending   FleetCapacity
	shapes    []FleetCapacity
}

func (d *demandInputs) addPending(shape FleetCapacity, n int) {
	d.pending = d.pending.Plus(shape.Times(n))
	if !slices.Contains(d.shapes, shape) {
		d.shapes = append(d.shapes, shape)
	}
}

// forecasts are each platform market's demand forecast and the demand
// pinned to a location: recent arrivals, scheduled runs and pending
// containers, with horizons from activation samples and the reserves that
// could meet them.
func forecasts(p Policy, r fleetRead, hosts []FleetHost, groups []pendingGroup) (map[ReserveMarket]MarketForecast, map[ReserveMarket][]LocationDemand) {
	stocked := map[string]bool{}
	for _, h := range hosts {
		if h.GPU != "" && h.State != FleetTerminating {
			stocked[h.GPU] = true
		}
	}
	markets := map[ReserveMarket]*demandInputs{}
	places := map[placementKey]*demandInputs{}
	var placeOrder []placementKey
	inputs := func(m ReserveMarket, region, zone string, shape FleetCapacity) []*demandInputs {
		if markets[m] == nil {
			markets[m] = &demandInputs{}
		}
		out := []*demandInputs{markets[m]}
		if region != "" || zone != "" {
			k := placementKey{market: m, region: region, zone: zone, shape: shape}
			if places[k] == nil {
				places[k] = &demandInputs{}
				placeOrder = append(placeOrder, k)
			}
			out = append(out, places[k])
		}
		return out
	}
	for _, a := range r.arrivals {
		var gpus []string
		if json.Unmarshal(a.Gpus, &gpus) != nil || a.Arrived == 0 {
			continue
		}
		m, ok := forecastMarket(p, gpus, a.GpuType, a.Preemptible, stocked)
		if !ok {
			continue
		}
		shape := FleetCapacity{CPUMillis: a.CpuMillis, MemoryBytes: a.MemoryBytes, GPUs: int(a.GpuCount)}
		arrival := Arrival{At: a.At, Shape: shape, Count: int(a.Arrived), Duration: time.Duration(a.ServedSeconds * float64(time.Second))}
		for _, d := range inputs(m, a.Region, a.Zone, shape) {
			d.arrivals = append(d.arrivals, arrival)
		}
	}
	for _, s := range r.scheduled {
		var gpus []string
		if json.Unmarshal(s.Gpus, &gpus) != nil {
			continue
		}
		m, ok := forecastMarket(p, gpus, "", s.Preemptible, stocked)
		if !ok {
			continue
		}
		cards := int(s.GpuCount)
		if cards == 0 && len(gpus) > 0 {
			cards = 1
		}
		shape := FleetCapacity{CPUMillis: s.CpuMillis, MemoryBytes: s.MemoryBytes, GPUs: cards}
		w := ScheduledWorkload{
			ID: s.WorkloadID, Shape: shape, Concurrency: int(s.Concurrency), MaxContainers: int(s.MaxContainers),
			Existing: min(int(s.LiveContainers), int(s.MinContainers)), AlwaysWarm: s.MinContainers > 0,
			KeepWarm: time.Duration(s.KeepWarmSeconds) * time.Second, Duration: time.Duration(s.RunSeconds * float64(time.Second)),
			Runs: []ScheduledRun{{At: s.NextFireAt, Count: 1}},
		}
		for _, d := range inputs(m, s.Region, s.Zone, shape) {
			d.workloads = append(d.workloads, w)
		}
	}
	for _, g := range groups {
		if g.connection != nil {
			continue
		}
		m, ok := forecastMarket(p, g.group.Need.GPUs, "", g.group.Need.Preemptible, stocked)
		if !ok {
			continue
		}
		shape := reservedShape(g.group.Need)
		for _, d := range inputs(m, g.group.Need.Region, g.group.Need.Zone, shape) {
			d.addPending(shape, len(g.group.Containers))
		}
	}
	forecast := func(m ReserveMarket, region string, d *demandInputs) MarketForecast {
		scheduled := ScheduledArrivals(d.workloads, r.now, r.until)
		return ForecastMarket(p, r.now, ForecastInput{
			Market: m, Region: region, Arrivals: d.arrivals, Scheduled: scheduled, Pending: d.pending, PendingShapes: d.shapes,
			Stats: r.stats, Hosts: hosts,
		})
	}
	out := map[ReserveMarket]MarketForecast{}
	for m, d := range markets {
		out[m] = forecast(m, "", d)
	}
	locations := map[ReserveMarket][]LocationDemand{}
	for _, k := range placeOrder {
		f := forecast(k.market, k.region, places[k])
		count := 0
		for _, dim := range [][2]int64{
			{f.Warm.CPUMillis, k.shape.CPUMillis}, {f.Warm.MemoryBytes, k.shape.MemoryBytes}, {int64(f.Warm.GPUs), int64(k.shape.GPUs)},
		} {
			if dim[1] > 0 {
				count = max(count, int((dim[0]+dim[1]-1)/dim[1]))
			}
		}
		if count > 0 {
			locations[k.market] = append(locations[k.market], LocationDemand{Region: k.region, Zone: k.zone, Shape: k.shape, Count: count})
		}
	}
	return out, locations
}

// offerCooldowns are the cooldowns of one owner: "platform" or a
// connection id.
func offerCooldowns(rows []PlannerCooldownsRow, owner string) []OfferCooldown {
	var out []OfferCooldown
	for _, r := range rows {
		if r.ConnectionKey != owner {
			continue
		}
		c := OfferCooldown{Region: r.Region, InstanceType: r.InstanceType, Market: Market(r.Market), Until: r.Until}
		if r.RefusedAt != nil {
			c.RefusedAt = *r.RefusedAt
		}
		out = append(out, c)
	}
	return out
}

// vcpuQuotas are the platform's known vCPU quotas as PlanFleet takes them:
// what each quota room leaves plus what the snapshot's hosts already count
// against it, which PlanFleet subtracts again.
func vcpuQuotas(rooms []QuotaRoom, hosts []FleetHost, catalog []CatalogType) []VCPUQuota {
	used := QuotaUse(hosts, catalog)
	var out []VCPUQuota
	for _, r := range rooms {
		if !r.Known {
			continue
		}
		key := QuotaKey{Region: r.Region, Class: r.Class, Market: r.Market}
		out = append(out, VCPUQuota{Key: key, VCPUs: r.VCPUs + used[key]})
	}
	return out
}

// plainStops are the region/type pairs whose hibernation booted cold within
// the last day (P1): their reserves stop plainly.
func plainStops(stats []ActivationStat) map[string]bool {
	out := map[string]bool{}
	for _, s := range stats {
		if s.Kind == ActivationResume && s.Outcome != nil && *s.Outcome == ResumeColdBoot && s.Ready > 0 {
			out[s.Region+"/"+s.InstanceType] = true
		}
	}
	return out
}
