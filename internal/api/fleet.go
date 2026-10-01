package api

import (
	"context"
	"errors"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// fleetPlanTTL is how long a fleet summary counts as current; the dashboard
// asks for a refresh after it.
const fleetPlanTTL = 5 * time.Minute

func (s *Server) administrator(ctx context.Context) error {
	p, ok := principalFrom(ctx)
	if !ok {
		return identity.ErrUnauthenticated
	}
	if !p.IsAdmin || p.TokenWorkspace != nil {
		return identity.ErrAdminRequired
	}
	return nil
}

func isNotFound(err error) bool { return errors.Is(err, compute.ErrNotFound) }

func fleetCapacityOut(c compute.FleetCapacity) apitypes.FleetCapacity {
	return apitypes.FleetCapacity{CpuMillicores: c.CPUMillis, MemoryMib: c.MemoryBytes >> 20, GpuCount: c.GPUs}
}

// GetFleet summarizes the platform fleet for administrators.
func (s *Server) GetFleet(ctx context.Context, _ GetFleetRequestObject) (GetFleetResponseObject, error) {
	if err := s.administrator(ctx); err != nil {
		return nil, err
	}
	fleet, err := s.owners.Compute.Fleet(ctx)
	if err != nil {
		return nil, err
	}
	out := GetFleet200JSONResponse{ObservedAt: fleet.ObservedAt}
	plan := &struct {
		ExpiresAt   time.Time              `json:"expires_at"`
		GeneratedAt time.Time              `json:"generated_at"`
		Markets     []apitypes.FleetMarket `json:"markets"`
	}{GeneratedAt: fleet.ObservedAt, ExpiresAt: fleet.ObservedAt.Add(fleetPlanTTL), Markets: []apitypes.FleetMarket{}}
	for _, m := range fleet.Markets {
		market := apitypes.FleetMarket{
			Preemptible: m.Preemptible, GpuType: m.GPUType, WarmFree: fleetCapacityOut(m.WarmFree),
			WarmTarget: fleetCapacityOut(m.WarmTarget), ReserveReady: fleetCapacityOut(compute.FleetCapacity{}),
			ReserveTarget: fleetCapacityOut(compute.FleetCapacity{}), Allocated: fleetCapacityOut(m.Allocated),
			States: []apitypes.FleetStateCapacity{}, Reason: m.Reason,
		}
		for _, st := range m.States {
			market.States = append(market.States, apitypes.FleetStateCapacity{
				State: apitypes.FleetState(st.State), Machines: st.Machines,
				Capacity: fleetCapacityOut(st.Capacity), Allocated: fleetCapacityOut(st.Allocated),
			})
		}
		plan.Markets = append(plan.Markets, market)
	}
	out.Plan = plan
	if r := fleet.Release; r != nil {
		out.Release = &struct {
			Complete              bool           `json:"complete"`
			Generation            int            `json:"generation"`
			PendingCapacityOwners int            `json:"pending_capacity_owners"`
			Phases                map[string]int `json:"phases"`
			Version               string         `json:"version"`
		}{Complete: r.Complete, Generation: 1, Phases: r.Phases, Version: r.Version}
	}
	return out, nil
}

// ListFleetNodes lists the platform's hosts for administrators.
func (s *Server) ListFleetNodes(ctx context.Context, req ListFleetNodesRequestObject) (ListFleetNodesResponseObject, error) {
	if err := s.administrator(ctx); err != nil {
		return nil, err
	}
	var after uuid.UUID
	if err := decodeCursor(req.Params.Cursor, &after); err != nil {
		return nil, err
	}
	limit := pageLimit(req.Params.Limit)
	nodes, err := s.owners.Compute.FleetNodes(ctx, after, limit+1)
	if err != nil {
		return nil, err
	}
	target, err := s.owners.Compute.TargetRelease(ctx)
	if err != nil && !isNotFound(err) {
		return nil, err
	}
	out := ListFleetNodes200JSONResponse{Nodes: []apitypes.FleetNode{}, ObservedAt: time.Now()}
	if len(nodes) > limit {
		nodes = nodes[:limit]
		out.NextCursor = encodeCursor(uuid.UUID(nodes[limit-1].ID))
	}
	for _, n := range nodes {
		node := apitypes.FleetNode{
			Id: uuid.UUID(n.ID), Provider: apitypes.FleetNodeProvider(n.Provider), Region: n.Region,
			InstanceType: n.InstanceType, Preemptible: n.Preemptible, GpuType: n.GPUType, State: apitypes.FleetState(n.State),
			Capacity: fleetCapacityOut(n.Capacity), Allocated: fleetCapacityOut(n.Allocated), Containers: n.Containers,
			Ready: n.State == compute.FleetServing && (target.Version == "" || n.AgentVersion == target.Version),
		}
		if n.Enrolled {
			id := n.ID.String()
			node.MachineId = &id
		}
		if n.InstanceID != "" {
			node.InstanceId = &n.InstanceID
		}
		out.Nodes = append(out.Nodes, node)
	}
	return out, nil
}
