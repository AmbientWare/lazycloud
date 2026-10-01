package compute

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/google/uuid"
)

// FleetState is a platform host's place in the fleet, as administrators see
// it.
type FleetState string

const (
	FleetServing     FleetState = "serving"
	FleetStarting    FleetState = "starting"
	FleetDraining    FleetState = "draining"
	FleetUnavailable FleetState = "unavailable"
	FleetFailed      FleetState = "failed"
	FleetTerminating FleetState = "terminating"
)

// FleetCapacity is CPU, memory and GPUs.
type FleetCapacity struct {
	CPUMillis   int64
	MemoryBytes int64
	GPUs        int
}

func (a *FleetCapacity) add(b FleetCapacity) {
	a.CPUMillis += b.CPUMillis
	a.MemoryBytes += b.MemoryBytes
	a.GPUs += b.GPUs
}

// FleetNode is one platform host.
type FleetNode struct {
	ID           HostID
	Enrolled     bool
	InstanceID   string
	Provider     Provider
	Region       string
	InstanceType string
	Preemptible  bool
	GPUType      string
	State        FleetState
	Capacity     FleetCapacity
	Allocated    FleetCapacity
	Containers   int
	AgentVersion string
}

// FleetStateCapacity totals one state's hosts in a market.
type FleetStateCapacity struct {
	State     FleetState
	Machines  int
	Capacity  FleetCapacity
	Allocated FleetCapacity
}

// FleetMarket is the platform's hosts of one purchase market and GPU model.
type FleetMarket struct {
	Preemptible bool
	GPUType     string
	WarmFree    FleetCapacity
	WarmTarget  FleetCapacity
	Allocated   FleetCapacity
	States      []FleetStateCapacity
	Reason      string
}

// FleetRelease is the agent rollout over platform hosts.
type FleetRelease struct {
	Version  string
	Complete bool
	// Phases counts connected hosts on the release (current), on another
	// version (updating) and disconnected ones (offline).
	Phases map[string]int
}

// FleetSummary is the platform fleet for administrators.
type FleetSummary struct {
	ObservedAt time.Time
	Markets    []FleetMarket
	Release    *FleetRelease
}

// fleetScan bounds the hosts one summary reads.
const fleetScan = 10000

// FleetNodes lists platform hosts by id after the cursor.
func (c *Compute) FleetNodes(ctx context.Context, after uuid.UUID, limit int) ([]FleetNode, error) {
	rows, err := c.queries.PlatformHosts(ctx, PlatformHostsParams{AfterID: after, MaxRows: int32(limit)}) //nolint:gosec // Bounded by the API or fleetScan.
	if err != nil {
		return nil, fmt.Errorf("list platform hosts: %w", err)
	}
	out := make([]FleetNode, 0, len(rows))
	for _, r := range rows {
		n := FleetNode{
			ID: HostID(r.ID), Enrolled: r.Enrolled, InstanceID: deref(r.InstanceID), Provider: Provider(r.Provider),
			Region: r.Region, InstanceType: r.InstanceType, Preemptible: deref(r.Market) == string(MarketSpot),
			GPUType: r.GpuType, Capacity: FleetCapacity{CPUMillis: r.CpuMillis, MemoryBytes: r.MemoryBytes, GPUs: int(r.GpuCount)},
			Allocated:  FleetCapacity{CPUMillis: r.UsedCpu, MemoryBytes: r.UsedMemory, GPUs: int(r.UsedGpus)},
			Containers: int(r.Containers), AgentVersion: r.AgentVersion,
		}
		switch Phase(r.Phase) {
		case PhaseTerminating, PhaseDeleted:
			n.State = FleetTerminating
		case PhaseFailed:
			n.State = FleetFailed
		case PhaseRequested, PhaseProvisioning, PhaseBooting, PhaseJoining:
			n.State = FleetStarting
		case PhaseDraining:
			n.State = FleetDraining
		case PhaseReady:
			switch {
			case CapacityState(r.CapacityState) != CapacityAvailable:
				n.State = FleetDraining
			case connected(r.State, r.LastSeenAt):
				n.State = FleetServing
			default:
				n.State = FleetUnavailable
			}
		default:
			n.State = FleetUnavailable
		}
		out = append(out, n)
	}
	return out, nil
}

// Fleet summarizes platform capacity per market: Spot and on-demand CPU
// always, and each GPU model the fleet runs.
func (c *Compute) Fleet(ctx context.Context) (FleetSummary, error) {
	nodes, err := c.FleetNodes(ctx, uuid.Nil, fleetScan)
	if err != nil {
		return FleetSummary{}, err
	}
	type key struct {
		preemptible bool
		gpu         string
	}
	markets := map[key]*FleetMarket{{true, ""}: {Preemptible: true}, {false, ""}: {}}
	states := map[key]map[FleetState]*FleetStateCapacity{}
	for _, n := range nodes {
		k := key{n.Preemptible, n.GPUType}
		m, ok := markets[k]
		if !ok {
			m = &FleetMarket{Preemptible: n.Preemptible, GPUType: n.GPUType}
			markets[k] = m
		}
		m.Allocated.add(n.Allocated)
		if n.State == FleetServing {
			m.WarmFree.add(FleetCapacity{
				CPUMillis: n.Capacity.CPUMillis - n.Allocated.CPUMillis, MemoryBytes: n.Capacity.MemoryBytes - n.Allocated.MemoryBytes,
				GPUs: n.Capacity.GPUs - n.Allocated.GPUs,
			})
			if n.Containers == 0 && c.fleet.HeadroomFloor > 0 {
				m.WarmTarget.add(n.Capacity)
			}
		}
		if states[k] == nil {
			states[k] = map[FleetState]*FleetStateCapacity{}
		}
		s := states[k][n.State]
		if s == nil {
			s = &FleetStateCapacity{State: n.State}
			states[k][n.State] = s
		}
		s.Machines++
		s.Capacity.add(n.Capacity)
		s.Allocated.add(n.Allocated)
	}
	summary := FleetSummary{ObservedAt: time.Now()}
	for k, m := range markets {
		for _, s := range states[k] {
			m.States = append(m.States, *s)
		}
		slices.SortFunc(m.States, func(a, b FleetStateCapacity) int { return strings.Compare(string(a.State), string(b.State)) })
		if len(c.fleet.Networks) == 0 {
			m.Reason = "the platform has no AWS network to launch in"
		}
		summary.Markets = append(summary.Markets, *m)
	}
	slices.SortFunc(summary.Markets, func(a, b FleetMarket) int {
		if a.GPUType != b.GPUType {
			return strings.Compare(a.GPUType, b.GPUType)
		}
		if a.Preemptible != b.Preemptible {
			if a.Preemptible {
				return -1
			}
			return 1
		}
		return 0
	})
	release, err := c.TargetRelease(ctx)
	if errors.Is(err, ErrNotFound) {
		return summary, nil
	}
	if err != nil {
		return FleetSummary{}, err
	}
	rollout := &FleetRelease{Version: release.Version, Complete: true, Phases: map[string]int{}}
	for _, n := range nodes {
		switch {
		case n.State != FleetServing && n.State != FleetDraining:
			if n.Enrolled {
				rollout.Phases["offline"]++
			}
		case n.AgentVersion == release.Version:
			rollout.Phases["current"]++
		default:
			rollout.Phases["updating"]++
			rollout.Complete = false
		}
	}
	summary.Release = rollout
	return summary, nil
}
