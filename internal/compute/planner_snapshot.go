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
)

// fleetRead is what one planning pass reads, all in its transaction.
type fleetRead struct {
	now       time.Time
	hosts     []PlannerHostsRow
	pending   []PendingDemandRow
	recent    []RecentShapesRow
	cooldowns []PlannerCooldownsRow
	markets   map[string]FleetMarketsRow
	// release is the target agent release; nil when none is published.
	release     *AgentRelease
	connections []HostingConnectionsRow
	spot        []SpotQuote
	zoneTypes   map[string]map[string][]string
	quotas      []QuotaRoom
}

// readFleet reads one pass's snapshot with a fixed number of statements,
// each bounded by live rows or the pending batch.
func readFleet(ctx context.Context, q *Queries, p Policy, now time.Time) (fleetRead, error) {
	r := fleetRead{now: now, markets: map[string]FleetMarketsRow{}}
	var err error
	if r.hosts, err = q.PlannerHosts(ctx); err != nil {
		return r, fmt.Errorf("read fleet hosts: %w", err)
	}
	if r.pending, err = q.PendingDemand(ctx, demandBatch); err != nil {
		return r, fmt.Errorf("read pending demand: %w", err)
	}
	if r.recent, err = q.RecentShapes(ctx, RecentShapesParams{
		WindowSeconds: p.LargestShape.Window.Seconds(), SampleSize: demandBatch,
	}); err != nil {
		return r, fmt.Errorf("read recent container shapes: %w", err)
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
	if r.spot, err = readSpotPrices(ctx, q); err != nil {
		return r, err
	}
	if r.quotas, err = quotaRooms(ctx, q, now); err != nil {
		return r, err
	}
	if r.zoneTypes, err = readZoneOfferings(ctx, q); err != nil {
		return r, err
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
		Load:       FleetCapacity{CPUMillis: h.UsedCpu, MemoryBytes: h.UsedMemory, GPUs: int(h.UsedGpus)},
		Containers: int(h.Containers), Protected: h.InterruptionAt != nil,
		Current: onRelease(HostID(h.ID), h.PreparedAgentVersion, release), ReserveMode: (*ReserveMode)(h.ReserveMode),
		HibernationConfigured: h.HibernationConfigured,
		Stoppable:             (market == MarketOnDemand || h.SpotRequestID != nil) && !refusedReserve(h),
		HourlyMicros:          h.HourlyMicros, IdleSince: h.IdleSince,
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

// largestShapes are the largest shape each market's placed platform
// containers reserved. GPU work belongs to the on-demand market of its
// model, as its demand does; GPU work without a recorded model counts in
// no market.
func largestShapes(rows []RecentShapesRow) map[ReserveMarket]FleetCapacity {
	out := map[ReserveMarket]FleetCapacity{}
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
		out[m] = out[m].Upper(FleetCapacity{CPUMillis: r.CpuMillis, MemoryBytes: r.MemoryBytes, GPUs: int(r.Gpus)})
	}
	return out
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
