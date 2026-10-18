package compute

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/cpu"
)

// offerRead is what offers rank from, read with a fixed number of
// statements, each bounded by live rows or a sample. A reserve whose start
// EC2 refused within cooldown reads as refused.
type offerRead struct {
	now       time.Time
	hosts []PlannerHostsRow
	// recent and quotas are by account key.
	recent    map[string][]RecentShapesRow
	cooldowns []PlannerCooldownsRow
	spot      []SpotQuote
	zoneTypes map[string]map[string][]string
	quotas    map[string][]QuotaRoom
	rates     []billing.ComputeRate
	// reported is the memory hosts of each type advertise, once one has
	// reported.
	reported map[string]int64
}

func readOffers(ctx context.Context, q *Queries, p Policy, now time.Time, cooldown time.Duration) (offerRead, error) {
	r := offerRead{now: now, reported: map[string]int64{}}
	var err error
	if r.hosts, err = q.PlannerHosts(ctx, cooldown.Seconds()); err != nil {
		return r, fmt.Errorf("read fleet hosts: %w", err)
	}
	for _, h := range r.hosts {
		if seen, ok := r.reported[h.InstanceType]; h.SessionEpoch > 0 && h.InstanceType != "" && (!ok || h.MemoryBytes < seen) {
			r.reported[h.InstanceType] = h.MemoryBytes
		}
	}
	recent, err := q.RecentShapes(ctx, recentShapesParams(p))
	if err != nil {
		return r, fmt.Errorf("read recent container shapes: %w", err)
	}
	r.recent = map[string][]RecentShapesRow{}
	for _, row := range recent {
		r.recent[row.Owner] = append(r.recent[row.Owner], row)
	}
	if r.cooldowns, err = q.PlannerCooldowns(ctx); err != nil {
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

// inputs are what an account's offers rank from in its networks with its
// hosts: its cooldowns, quotas and demand.
func (r offerRead) inputs(storageRegion string, a account, hosts []FleetHost) OfferInputs {
	zones := map[string]int{}
	for _, h := range hosts {
		if h.State != FleetTerminating && h.ZoneID != "" {
			zones[h.ZoneID]++
		}
	}
	return OfferInputs{
		Now: r.now, Catalog: FleetCatalog(), Networks: a.networks, Spot: r.spot, Cooldowns: offerCooldowns(r.cooldowns, a.key()),
		ReportedMemory: r.reported, ZoneHosts: zones, ZoneTypes: r.zoneTypes, StorageRegion: storageRegion, Rates: r.rates,
		Quotas: vcpuQuotas(r.quotas[a.key()]), ReadBytes: readsByMarket(r.recent[a.key()]), OwnerPays: a.pays,
	}
}

// fleetRead is what one planning pass reads, all in its transaction.
type fleetRead struct {
	offerRead
	pending  []PendingDemandRow
	markets  map[string]FleetMarketsRow
	accounts []account
	// batchWaits are how long each account's arrival batch stays open, by
	// account key.
	batchWaits map[string]time.Duration
}

// readFleet reads one pass's snapshot with a fixed number of statements,
// each bounded by live rows or the pending batch.
func (c *Compute) readFleet(ctx context.Context, q *Queries, p Policy, now time.Time) (fleetRead, error) {
	r, err := readMarkets(ctx, q, p, now, c.fleet.CapacityCooldown)
	if err != nil {
		return r, err
	}
	if r.accounts, err = c.accounts(ctx, q); err != nil {
		return r, err
	}
	return r, nil
}

// readMarkets reads what a pass plans every account from.
func readMarkets(ctx context.Context, q *Queries, p Policy, now time.Time, cooldown time.Duration) (fleetRead, error) {
	offers, err := readOffers(ctx, q, p, now, cooldown)
	if err != nil {
		return fleetRead{}, err
	}
	r := fleetRead{offerRead: offers, markets: map[string]FleetMarketsRow{}, batchWaits: map[string]time.Duration{}}
	if r.pending, err = q.PendingDemand(ctx, demandBatch); err != nil {
		return r, fmt.Errorf("read pending demand: %w", err)
	}
	waits, err := q.BatchWaits(ctx, BatchWaitsParams{
		SampleSize: demandBatch, QuietSeconds: p.Batch.Quiet.Seconds(), MaxSeconds: p.Batch.Max.Seconds(),
		LookbackSeconds: (p.Batch.Max + p.Batch.Quiet).Seconds(),
	})
	if err != nil {
		return r, fmt.Errorf("read the arrival batches: %w", err)
	}
	for _, w := range waits {
		r.batchWaits[account{connection: w.ConnectionID}.key()] = time.Duration(w.WaitSeconds * float64(time.Second))
	}
	markets, err := q.FleetMarkets(ctx)
	if err != nil {
		return r, fmt.Errorf("read fleet markets: %w", err)
	}
	for _, m := range markets {
		r.markets[m.Market] = m
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
func fleetHostOf(h PlannerHostsRow, now time.Time) FleetHost {
	market := marketOf(h.Market)
	moves := make([]Requirement, len(h.MoveCpu))
	for n := range moves {
		moves[n] = Requirement{Region: h.MoveRegion[n], Preemptible: true, GPUCount: int(h.MoveGpuCount[n]),
			CPUMillis: cpu.Millis(h.MoveCpu[n]), MemoryBytes: h.MoveMemory[n]}
		if h.MoveGpus[n] != "" {
			moves[n].GPUs = strings.Split(h.MoveGpus[n], ",")
		}
	}
	return FleetHost{
		ID: HostID(h.ID), InstanceType: h.InstanceType, Region: h.Region, Zone: h.AvailabilityZone, ZoneID: h.AvailabilityZoneID,
		Market: market, GPU: h.GpuType, State: fleetStateOf(h.standing(), now),
		Usable:     FleetCapacity{CPUMillis: h.CpuMillis, MemoryBytes: h.MemoryBytes, GPUs: int(h.GpuCount)},
		Load:       FleetCapacity{CPUMillis: cpu.Millis(h.UsedCpu), MemoryBytes: h.UsedMemory, GPUs: int(h.UsedGpus)},
		Containers: int(h.Containers), UsedDisks: int(h.UsedDisks), Protected: h.InterruptionAt != nil,
		Requested: Phase(h.Phase) == PhaseRequested,
		UpdateDue: h.UpdateDue, ReserveMode: (*ReserveMode)(h.ReserveMode),
		HibernationConfigured: h.HibernationConfigured,
		Stoppable:             (market == MarketOnDemand || h.SpotRequestID != nil) && !refusedReserve(h),
		HourlyMicros:          h.HourlyMicros, PhaseAt: h.PhaseAt,
		StartRefused: h.StartRefused, Replaces: (*HostID)(h.Replaces), Slept: h.PreparedAgentVersion != nil, RightsizeRefusedAt: h.RightsizeRefusedAt,
		HandsBack: h.HandsBackAt != nil, Moves: moves, Stays: h.Stays, StoppedAt: h.StoppedAt,
		Packed: FleetCapacity{CPUMillis: cpu.Millis(h.PackedCpu), MemoryBytes: h.PackedMemory},
	}
}

// refusedReserve reports a host serving after its agent refused to prove a
// stop into the reserve.
func refusedReserve(h PlannerHostsRow) bool {
	return Phase(h.Phase) == PhaseReady && h.ReserveMode != nil
}

// stuckPreparing reports a host whose agent has not proved it may
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
			CPUMillis: cpu.Millis(r.CpuMillis), MemoryBytes: r.MemoryBytes, Disks: int(r.Disks),
			Read: VolumeReadOf(r.ReadRegion, r.ReadBytes), Packs: r.Packs,
		}}}
		for n, id := range r.Ids {
			c := PendingContainer{ID: id, Since: r.Created[n]}
			if r.Bought[n] != uuid.Nil {
				c.Host = ptr(HostID(r.Bought[n]))
			}
			g.group.Containers = append(g.group.Containers, c)
		}
		out = append(out, g)
	}
	return out, nil
}

func recentShapesParams(p Policy) RecentShapesParams {
	return RecentShapesParams{
		WindowSeconds: p.LargestShape.Window.Seconds(), SampleSize: demandBatch, ArrivalSeconds: p.ArrivalWindow.Seconds(), WarmSeconds: p.WarmWindow.Seconds(),
		BatchQuietSeconds: p.Batch.Quiet.Seconds(), BatchMaxSeconds: p.Batch.Max.Seconds(),
	}
}

// market is the market a RecentShapes row measures: GPU work belongs to
// its model's market of its tolerance, as its demand does, and GPU work
// without a model to none.
func (r RecentShapesRow) market() (ReserveMarket, bool) {
	switch {
	case r.GpuType != "":
		return ReserveMarket{Preemptible: r.Preemptible, GPU: r.GpuType}, true
	case r.Gpus > 0:
		return ReserveMarket{}, false
	}
	return ReserveMarket{Preemptible: r.Preemptible}, true
}

// readsByMarket is what a start of each market read from its workspace's
// bucket on average over its recent starts, for the markets whose starts
// read any.
func readsByMarket(rows []RecentShapesRow) map[ReserveMarket]int64 {
	reads := map[ReserveMarket]int64{}
	for _, r := range rows {
		if m, ok := r.market(); ok && shapeKind(r.Kind) == shapeRecent && r.ReadBytes > 0 && r.Starts > 0 {
			reads[m] = r.ReadBytes / int64(r.Starts)
		}
	}
	return reads
}

// shapeKind is what a RecentShapes row measures.
type shapeKind string

const (
	shapeRecent  shapeKind = "recent"
	shapeStarted shapeKind = "started"
	shapeBurst   shapeKind = "burst"
	shapeArrived shapeKind = "arrived"
)

// shapesByMarket are, by market, the largest shape placed containers
// reserved and the most disks one attached, and what arrived
// within the arrival window.
func shapesByMarket(rows []RecentShapesRow) (recent, started, burst, arrived map[ReserveMarket]FleetCapacity, disks map[ReserveMarket]int) {
	recent, started, burst, arrived = map[ReserveMarket]FleetCapacity{}, map[ReserveMarket]FleetCapacity{}, map[ReserveMarket]FleetCapacity{}, map[ReserveMarket]FleetCapacity{}
	disks = map[ReserveMarket]int{}
	for _, r := range rows {
		m, ok := r.market()
		if !ok {
			continue
		}
		shape := FleetCapacity{CPUMillis: cpu.Millis(r.CpuMillis), MemoryBytes: r.MemoryBytes, GPUs: int(r.Gpus)}
		switch shapeKind(r.Kind) {
		case shapeRecent:
			recent[m], disks[m] = recent[m].Upper(shape), max(disks[m], int(r.Disks))
		case shapeStarted:
			started[m] = started[m].Upper(shape)
		case shapeBurst:
			burst[m] = burst[m].Upper(shape)
		case shapeArrived:
			arrived[m] = arrived[m].Plus(shape)
		}
	}
	return recent, started, burst, arrived, disks
}

// offerCooldowns are the cooldowns of one account, by its key.
func offerCooldowns(rows []PlannerCooldownsRow, owner string) []OfferCooldown {
	var out []OfferCooldown
	for _, r := range rows {
		if r.ConnectionKey != owner {
			continue
		}
		out = append(out, OfferCooldown{Region: r.Region, ZoneID: r.AvailabilityZoneID, InstanceType: r.InstanceType, Market: Market(r.Market), Until: r.Until})
	}
	return out
}

// vcpuQuotas are an account's known vCPU quotas as PlanFleet takes them;
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
