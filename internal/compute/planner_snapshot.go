package compute

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/cpu"
)

// offerRead is what every owner's hosts and offer inputs derive from, read
// once a pass by the planner and the launcher alike.
type offerRead struct {
	now       time.Time
	hosts     []PlannerHostsRow
	cooldowns []PlannerCooldownsRow
	spot      []SpotQuote
	zoneTypes map[string]map[string][]string
	quotas    []QuotaRoom
	rates     []billing.ComputeRate
	// reported is the least memory hosts of each type advertise, once one
	// has reported.
	reported map[string]int64
}

// readOffers reads the offer inputs of every owner at now.
func readOffers(ctx context.Context, q *Queries, p Policy, now time.Time) (offerRead, error) {
	r := offerRead{now: now, reported: map[string]int64{}}
	var err error
	if r.hosts, err = q.PlannerHosts(ctx); err != nil {
		return r, fmt.Errorf("read fleet hosts: %w", err)
	}
	for _, h := range r.hosts {
		if h.SessionEpoch > 0 && h.InstanceType != "" {
			if seen, ok := r.reported[h.InstanceType]; !ok || h.MemoryBytes < seen {
				r.reported[h.InstanceType] = h.MemoryBytes
			}
		}
	}
	if r.cooldowns, err = q.PlannerCooldowns(ctx, p.RegionFailureWindow.Seconds()); err != nil {
		return r, fmt.Errorf("read cooldowns: %w", err)
	}
	if r.spot, err = readSpotPrices(ctx, q); err != nil {
		return r, err
	}
	if r.quotas, err = quotaRooms(ctx, q, now); err != nil {
		return r, err
	}
	if r.zoneTypes, err = readZoneOfferings(ctx, q); err != nil {
		return r, err
	}
	if r.rates, err = billing.FleetComputeRates(now); err != nil {
		return r, fmt.Errorf("read the fleet's compute rates: %w", err)
	}
	return r, nil
}

// ownerOffers are one owner's hosts and the inputs its offers rank from.
type ownerOffers struct {
	hosts []FleetHost
	// stuck are the platform's hosts preparing past preparingLimit, which
	// hosts leaves out; unproven cools the offer of each platform host that
	// could not prove a stop into the reserve, so a pass does not buy its
	// replacement from it.
	stuck    []uuid.UUID
	unproven []OfferCooldown
	in       OfferInputs
}

// owner is the hosts and offer inputs of the platform, for a nil
// connection, or of connection, buying in networks. Only the platform pays
// rates and holds quotas; a connection's account pays its own hosts. A
// host bought for the reserve that refused to prove a stop cools its offer
// for cooldown.
func (r offerRead) owner(connection *uuid.UUID, networks map[string]Network, release *AgentRelease, cooldown time.Duration) ownerOffers {
	key := ownerPlatform
	if connection != nil {
		key = connection.String()
	}
	var o ownerOffers
	zones := map[string]int{}
	for _, row := range r.hosts {
		if connection == nil && HostKind(row.Kind) != KindPlatform || connection != nil && (row.ConnectionID == nil || *row.ConnectionID != *connection) {
			continue
		}
		stuck := connection == nil && stuckPreparing(row, r.now)
		if stuck || connection == nil && refusedReserve(row) && row.Containers == 0 && r.now.Sub(row.PhaseAt) < cooldown {
			o.unproven = append(o.unproven, OfferCooldown{
				Region: row.Region, InstanceType: row.InstanceType, Market: marketOf(row.Market), Until: r.now.Add(cooldown),
			})
		}
		if stuck {
			o.stuck = append(o.stuck, row.ID)
			continue
		}
		h := fleetHostOf(row, r.now, release)
		if h.State != FleetTerminating && h.ZoneID != "" {
			zones[h.ZoneID]++
		}
		o.hosts = append(o.hosts, h)
	}
	catalog := FleetCatalog()
	o.in = OfferInputs{
		Now: r.now, Catalog: catalog, Networks: networks, Spot: r.spot,
		Cooldowns: append(offerCooldowns(r.cooldowns, key), o.unproven...), ReportedMemory: r.reported, ZoneHosts: zones,
		ZoneTypes: r.zoneTypes, QuotaUsed: QuotaUse(o.hosts, catalog), OwnerPays: connection != nil,
	}
	if connection == nil {
		o.in.Rates, o.in.Quotas = r.rates, vcpuQuotas(r.quotas)
	}
	return o
}

// fleetRead is what one planning pass reads, all in its transaction.
type fleetRead struct {
	offerRead
	pending []PendingDemandRow
	recent  []RecentShapesRow
	markets map[string]FleetMarketsRow
	// release is the target agent release; nil when none is published.
	release     *AgentRelease
	connections []HostingConnectionsRow
	// batchWait is how long the platform's arrival batch stays open, and
	// connectionWaits each connection's.
	batchWait       time.Duration
	connectionWaits map[uuid.UUID]time.Duration
}

// readFleet reads one pass's snapshot with a fixed number of statements,
// each bounded by live rows or the pending batch.
func readFleet(ctx context.Context, q *Queries, p Policy, now time.Time) (fleetRead, error) {
	r := fleetRead{markets: map[string]FleetMarketsRow{}}
	var err error
	if r.offerRead, err = readOffers(ctx, q, p, now); err != nil {
		return r, err
	}
	if r.pending, err = q.PendingDemand(ctx, demandBatch); err != nil {
		return r, fmt.Errorf("read pending demand: %w", err)
	}
	if r.recent, err = q.RecentShapes(ctx, RecentShapesParams{
		WindowSeconds: p.LargestShape.Window.Seconds(), SampleSize: demandBatch, BuildWindowSeconds: p.BuildWindow.Seconds(),
	}); err != nil {
		return r, fmt.Errorf("read recent container shapes: %w", err)
	}
	arrivals, err := q.BatchArrivals(ctx, BatchArrivalsParams{SampleSize: demandBatch, LookbackSeconds: p.Batch.lookback().Seconds()})
	if err != nil {
		return r, fmt.Errorf("read the arrival batches: %w", err)
	}
	var platform []time.Duration
	connections := map[uuid.UUID][]time.Duration{}
	for _, a := range arrivals {
		age := time.Duration(a.AgeSeconds * float64(time.Second))
		if a.ConnectionID == nil {
			platform = append(platform, age)
			continue
		}
		connections[*a.ConnectionID] = append(connections[*a.ConnectionID], age)
	}
	r.batchWait = p.Batch.wait(platform)
	r.connectionWaits = map[uuid.UUID]time.Duration{}
	for id, ages := range connections {
		r.connectionWaits[id] = p.Batch.wait(ages)
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
	return r, nil
}

// hostStanding is what a cloud host's fleet state derives from, for the
// planner and the admin Nodes list alike.
type hostStanding struct {
	Phase, State, CapacityState, ImageEvidence string
	LastSeenAt                                 *time.Time
	ReserveMode                                *string
}

func (h PlannerHostsRow) standing() hostStanding {
	return hostStanding{
		Phase: h.Phase, State: h.State, CapacityState: h.CapacityState, ImageEvidence: h.ImageEvidence,
		LastSeenAt: h.LastSeenAt, ReserveMode: h.ReserveMode,
	}
}

// fleetStateOf is where a cloud host stands in the fleet. A host bought for
// the reserve or refreshing prepares until it stops.
func fleetStateOf(h hostStanding, now time.Time) FleetState {
	reserve := h.ReserveMode != nil
	switch Phase(h.Phase) {
	case PhaseRequested, PhaseProvisioning, PhaseBooting, PhaseJoining, PhaseResuming:
		if reserve {
			return FleetPreparing
		}
		return FleetStarting
	case PhaseReady:
		if CapacityState(h.CapacityState) != CapacityAvailable {
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
	case PhaseTerminating, PhaseDeleted, PhaseFailed:
		// The planner and the Nodes list read no deleted or failed host.
		return FleetTerminating
	}
	return FleetUnavailable
}

// fleetHostOf is a cloud host as PlanFleet sees it. Only an on-demand host
// or a Spot host on a persistent request can stop. A host whose agent
// refused to prove a stop serves with its reserve mode still set; it is not
// asked again, so retention drains it.
func fleetHostOf(h PlannerHostsRow, now time.Time, release *AgentRelease) FleetHost {
	market := marketOf(h.Market)
	return FleetHost{
		ID: HostID(h.ID), InstanceType: h.InstanceType, Region: h.Region, Zone: h.AvailabilityZone, ZoneID: h.AvailabilityZoneID,
		Market: market, GPU: h.GpuType, State: fleetStateOf(h.standing(), now),
		Usable:     FleetCapacity{CPUMillis: h.CpuMillis, MemoryBytes: h.MemoryBytes, GPUs: int(h.GpuCount)},
		Load:       FleetCapacity{CPUMillis: cpu.Millis(h.UsedCpu), MemoryBytes: h.UsedMemory, GPUs: int(h.UsedGpus)},
		Containers: int(h.Containers), Protected: h.InterruptionAt != nil,
		Current: onRelease(HostID(h.ID), h.PreparedAgentVersion, release), ReserveMode: (*ReserveMode)(h.ReserveMode),
		HibernationConfigured: h.HibernationConfigured,
		Stoppable:             (market == MarketOnDemand || h.SpotRequestID != nil) && !refusedReserve(h),
		HourlyMicros:          h.HourlyMicros, IdleSince: h.IdleSince, PhaseAt: h.PhaseAt,
	}
}

// onRelease reports whether version is the agent release a host should run:
// the target once the rollout reaches it, any release outside the rollout
// or without a target. A reserve passes the release it last proved it could
// stop on.
func onRelease(host HostID, version *string, release *AgentRelease) bool {
	if release == nil || rolloutBucket(host) >= release.RolloutPercent {
		return true
	}
	return version != nil && *version == release.Version
}

// refusedReserve reports a host serving after its agent refused to prove a
// stop into the reserve.
func refusedReserve(h PlannerHostsRow) bool {
	return Phase(h.Phase) == PhaseReady && h.ReserveMode != nil
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
			CPUMillis: cpu.Millis(r.CpuMillis), MemoryBytes: r.MemoryBytes,
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

// largestShapes are the largest shape each market's placed platform
// containers reserved, and the largest its builds did. GPU work belongs to
// the on-demand market of its model, as its demand does; GPU work without
// a recorded model counts in no market.
func largestShapes(rows []RecentShapesRow) (recent, builds map[ReserveMarket]FleetCapacity) {
	recent, builds = map[ReserveMarket]FleetCapacity{}, map[ReserveMarket]FleetCapacity{}
	for _, r := range rows {
		var m ReserveMarket
		switch {
		case r.GpuType != "":
			m = ReserveMarket{GPU: r.GpuType}
		case r.Gpus > 0:
			continue
		default:
			m = ReserveMarket{Preemptible: r.Preemptible}
		}
		out := recent
		if r.Build {
			out = builds
		}
		out[m] = out[m].Upper(FleetCapacity{CPUMillis: cpu.Millis(r.CpuMillis), MemoryBytes: r.MemoryBytes, GPUs: int(r.Gpus)})
	}
	return recent, builds
}

// offerCooldowns are the cooldowns of one owner: "platform" or a
// connection id.
func offerCooldowns(rows []PlannerCooldownsRow, owner string) []OfferCooldown {
	var out []OfferCooldown
	for _, r := range rows {
		if r.ConnectionKey != owner {
			continue
		}
		c := OfferCooldown{Region: r.Region, ZoneID: r.AvailabilityZoneID, InstanceType: r.InstanceType, Market: Market(r.Market), Until: r.Until}
		if r.RefusedAt != nil {
			c.RefusedAt = *r.RefusedAt
		}
		out = append(out, c)
	}
	return out
}

// vcpuQuotas are the platform's known vCPU quotas as PlanFleet takes them;
// it subtracts what the snapshot's hosts run.
func vcpuQuotas(rooms []QuotaRoom) []VCPUQuota {
	var out []VCPUQuota
	for _, r := range rooms {
		if r.Known {
			out = append(out, VCPUQuota{Key: QuotaKey{Region: r.Region, Class: r.Class, Market: r.Market}, VCPUs: r.VCPUs})
		}
	}
	return out
}
