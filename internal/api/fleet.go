package api

import (
	"context"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

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

func fleetCapacityOut(c compute.FleetCapacity) apitypes.FleetCapacity {
	return apitypes.FleetCapacity{CpuMillicores: c.CPUMillis, MemoryMib: c.MemoryBytes >> 20, GpuCount: c.GPUs}
}

// GetFleet returns the platform fleet's published plan and agent rollout
// for administrators. The plan is absent while none is current.
func (s *Server) GetFleet(ctx context.Context, _ GetFleetRequestObject) (GetFleetResponseObject, error) {
	if err := s.administrator(ctx); err != nil {
		return nil, err
	}
	fleet, err := s.owners.Compute.Fleet(ctx)
	if err != nil {
		return nil, err
	}
	out := GetFleet200JSONResponse{ObservedAt: fleet.ObservedAt}
	if p := fleet.Plan; p != nil {
		plan := &struct {
			ExpiresAt   time.Time              `json:"expires_at"`
			GeneratedAt time.Time              `json:"generated_at"`
			Markets     []apitypes.FleetMarket `json:"markets"`
		}{GeneratedAt: p.GeneratedAt, ExpiresAt: p.ExpiresAt, Markets: []apitypes.FleetMarket{}}
		for _, m := range p.Markets {
			market := apitypes.FleetMarket{
				Preemptible: m.Preemptible, GpuType: m.GPUType, WarmFree: fleetCapacityOut(m.WarmFree),
				WarmTarget: fleetCapacityOut(m.WarmTarget), ReserveReady: fleetCapacityOut(m.ReserveReady),
				ReserveTarget: fleetCapacityOut(m.StoppedTarget), Allocated: fleetCapacityOut(m.Load),
				States: []apitypes.FleetStateCapacity{}, Reason: string(m.Reason),
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
	}
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

// ListFleetNodes lists the platform's hosts with an instance for
// administrators.
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
			Ready: n.Ready, InstanceId: &n.InstanceID,
		}
		if n.Enrolled {
			id := n.ID.String()
			node.MachineId = &id
		}
		out.Nodes = append(out.Nodes, node)
	}
	return out, nil
}
