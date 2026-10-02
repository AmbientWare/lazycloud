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
	CPUMillis   int64 `json:"cpu_millis"`
	MemoryBytes int64 `json:"memory_bytes"`
	GPUs        int   `json:"gpus"`
}

// FleetNode is one platform host with an instance.
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
	// Ready is a serving host on the agent release it should run, or a
	// stopped reserve prepared for that release.
	Ready bool
}

// FleetStateCapacity totals one state's hosts in a market.
type FleetStateCapacity struct {
	State     FleetState
	Machines  int
	Capacity  FleetCapacity
	Allocated FleetCapacity
}

// FleetRelease is the agent rollout over platform hosts.
type FleetRelease struct {
	Version  string
	Complete bool
	// Phases counts connected hosts on the release (current), on another
	// version (updating), reserves (reserve) and other enrolled hosts
	// (offline).
	Phases map[string]int
}

// FleetPublication is the plan the fleet planning pass last published,
// while it is current.
type FleetPublication struct {
	GeneratedAt time.Time
	ExpiresAt   time.Time
	Markets     []PublishedMarket
}

// FleetSummary is the platform fleet for administrators.
type FleetSummary struct {
	ObservedAt time.Time
	// Plan is nil while no market has a current plan.
	Plan    *FleetPublication
	Release *FleetRelease
}

// FleetNodes lists platform hosts with an instance by id after the cursor.
func (c *Compute) FleetNodes(ctx context.Context, after uuid.UUID, limit int) ([]FleetNode, error) {
	release, err := c.TargetRelease(ctx)
	if err != nil && !errors.Is(err, ErrNotFound) {
		return nil, err
	}
	var target *AgentRelease
	if err == nil {
		target = &release
	}
	rows, err := c.queries.PlatformHosts(ctx, PlatformHostsParams{AfterID: after, MaxRows: int32(limit)}) //nolint:gosec // The API bounds the page.
	if err != nil {
		return nil, fmt.Errorf("list platform hosts: %w", err)
	}
	now := time.Now()
	out := make([]FleetNode, 0, len(rows))
	for _, r := range rows {
		n := FleetNode{
			ID: HostID(r.ID), Enrolled: r.Enrolled, InstanceID: r.InstanceID, Provider: Provider(r.Provider),
			Region: r.Region, InstanceType: r.InstanceType, Preemptible: deref(r.Market) == string(MarketSpot),
			GPUType: r.GpuType, Capacity: FleetCapacity{CPUMillis: r.CpuMillis, MemoryBytes: r.MemoryBytes, GPUs: int(r.GpuCount)},
			Allocated:  FleetCapacity{CPUMillis: r.UsedCpu, MemoryBytes: r.UsedMemory, GPUs: int(r.UsedGpus)},
			Containers: int(r.Containers),
			State: fleetStateOf(hostStanding{
				Phase: r.Phase, State: r.State, CapacityState: r.CapacityState, CapacityReason: r.CapacityReason,
				ImageEvidence: r.ImageEvidence, LastSeenAt: r.LastSeenAt, ReserveMode: r.ReserveMode, Containers: r.Containers,
			}, now),
		}
		switch n.State {
		case FleetServing:
			n.Ready = onRelease(n.ID, &r.AgentVersion, target)
		case FleetStopped, FleetHibernateUnverified, FleetImageSaved:
			n.Ready = onRelease(n.ID, r.PreparedAgentVersion, target)
		case FleetStarting, FleetDraining, FleetPreparing, FleetStopping, FleetUnavailable, FleetFailed, FleetTerminating:
		}
		out = append(out, n)
	}
	return out, nil
}

// Fleet returns the platform fleet's current published plan, CPU markets
// first, and the agent rollout. A market whose plan expired is left out.
func (c *Compute) Fleet(ctx context.Context) (FleetSummary, error) {
	summary := FleetSummary{ObservedAt: time.Now()}
	markets, err := c.PublishedPlan(ctx)
	if err != nil {
		return FleetSummary{}, err
	}
	// One reserve pass publishes every market it plans, so the plan is the
	// newest pass's markets; a market it no longer plans keeps an older row.
	var newest time.Time
	for _, m := range markets {
		if m.GeneratedAt.After(newest) {
			newest = m.GeneratedAt
		}
	}
	markets = slices.DeleteFunc(markets, func(m PublishedMarket) bool {
		return !m.GeneratedAt.Equal(newest) || !summary.ObservedAt.Before(m.ExpiresAt)
	})
	if len(markets) > 0 {
		summary.Plan = &FleetPublication{GeneratedAt: newest, ExpiresAt: markets[0].ExpiresAt, Markets: markets}
	}
	slices.SortFunc(markets, func(a, b PublishedMarket) int {
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
	rows, err := c.queries.FleetRollout(ctx, FleetRolloutParams{
		LiveAfter: summary.ObservedAt.Add(-LivenessTimeout), Consolidating: reasonConsolidating, Version: release.Version,
	})
	if err != nil {
		return FleetSummary{}, fmt.Errorf("count the agent rollout: %w", err)
	}
	rollout := &FleetRelease{Version: release.Version, Phases: map[string]int{}}
	for _, r := range rows {
		if r.Phase != "" {
			rollout.Phases[r.Phase] = int(r.Hosts)
		}
	}
	rollout.Complete = rollout.Phases["updating"] == 0
	summary.Release = rollout
	return summary, nil
}
