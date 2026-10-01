package compute

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"slices"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// demandBatch bounds the pending containers one capacity pass considers.
const demandBatch = 2000

// CapacityWait says why a pending container waits for compute.
type CapacityWait string

const (
	// WaitProvisioning means a host being bought will take it.
	WaitProvisioning CapacityWait = "provisioning"
	// WaitLimit means the fleet limit holds the purchase back.
	WaitLimit CapacityWait = "limit"
)

// CapacityResult summarizes one capacity pass.
type CapacityResult struct {
	// Skipped means another controller held the capacity lock.
	Skipped bool
	// Requested counts hosts inserted for launch.
	Requested int
	// Limited counts containers held back by the fleet limit.
	Limited int
}

type demandItem struct {
	id   uuid.UUID
	need Requirement
	wait *string
	// bought is the host bought for the container, with its phase and
	// offer.
	bought      *uuid.UUID
	boughtPhase Phase
	boughtOffer [3]string
}

// PlanCapacity buys what pending containers need. Under the capacity lock it
// simulates every pending container on ready hosts' free capacity and then
// on hosts already being bought; what still fits nowhere is packed
// first-fit-decreasing onto new hosts of the cheapest offer that takes each
// container, within the fleet limit of its owner. It inserts the new hosts
// as requested, for the launcher, and records on each container whether a
// purchase or the limit holds it.
func (c *Compute) PlanCapacity(ctx context.Context, logger *slog.Logger) (CapacityResult, error) {
	var result CapacityResult
	var requested []uuid.UUID
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		result, requested = CapacityResult{}, nil
		q := c.queries.WithTx(tx)
		locked, err := q.TryCapacityLock(ctx)
		if err != nil {
			return fmt.Errorf("try capacity lock: %w", err)
		}
		if !locked {
			result.Skipped = true
			return nil
		}
		demand, err := pendingDemandOf(ctx, q)
		if err != nil || len(demand) == 0 {
			return err
		}
		ready, err := AvailableCapacity(ctx, tx)
		if err != nil {
			return err
		}
		inflight, err := q.InFlightHosts(ctx)
		if err != nil {
			return fmt.Errorf("read in-flight hosts: %w", err)
		}
		coming := make([]HostCapacity, 0, len(inflight))
		for _, h := range inflight {
			coming = append(coming, HostCapacity{
				Host: HostID(h.ID), Kind: HostKind(h.Kind), Provider: ProviderAWS, Connection: h.ConnectionID,
				Region: h.Region, Zone: h.AvailabilityZone, ZoneID: h.AvailabilityZoneID, Market: marketOf(h.Market),
				GPUType: h.GpuType, GPUCount: int(h.GpuCount), CPUMillis: h.CpuMillis, MemoryBytes: h.MemoryBytes,
				FreeCPUMillis: h.CpuMillis, FreeMemoryBytes: h.MemoryBytes, FreeGPUs: int(h.GpuCount),
			})
		}
		plan, err := c.planner(ctx, q)
		if err != nil {
			return err
		}
		// Largest first: big containers claim holes before small ones
		// fragment them.
		slices.SortStableFunc(demand, func(a, b demandItem) int { return size(b.need) - size(a.need) })
		waits := make([]string, len(demand))
		hosts := make([]uuid.UUID, len(demand))
		var short []int
		for n, d := range demand {
			switch {
			case d.need.Machine != "":
				// A pinned machine joins by itself; nothing is bought.
			case fitFirst(ready, d.need) >= 0:
			case d.bought != nil && inFlight(d.boughtPhase):
				// Its host is on the way; buying another would double it.
				waits[n], hosts[n] = string(WaitProvisioning), *d.bought
				for i := range coming {
					if uuid.UUID(coming[i].Host) == *d.bought {
						coming[i].Reserve(d.need)
					}
				}
			default:
				if i := fitFirst(coming, d.need); i >= 0 {
					waits[n], hosts[n] = string(WaitProvisioning), uuid.UUID(coming[i].Host)
					continue
				}
				if d.bought != nil && d.boughtPhase == PhaseReady {
					// The host bought for it joined and still cannot take
					// it, so the offer is not what it predicted: cool it.
					if err := plan.coolDown(ctx, q, d); err != nil {
						return err
					}
				}
				short = append(short, n)
			}
		}
		needs := make([]Requirement, len(short))
		for i, n := range short {
			needs[i] = demand[n].need
		}
		boughtFor := map[int]int{}
		for i, n := range short {
			wait, bin := plan.buy(needs[i], needs[i+1:])
			waits[n] = string(wait)
			if bin >= 0 {
				boughtFor[n] = bin
			}
			if wait == WaitLimit {
				result.Limited++
			}
		}
		for _, h := range plan.bought {
			id, err := q.InsertRequestedHost(ctx, h)
			if err != nil {
				return fmt.Errorf("insert requested host: %w", err)
			}
			requested = append(requested, id)
		}
		for n, bin := range boughtFor {
			hosts[n] = requested[bin]
		}
		result.Requested = len(plan.bought)
		ids := make([]uuid.UUID, len(demand))
		for n, d := range demand {
			ids[n] = d.id
		}
		if _, err := q.SetCapacityWaits(ctx, SetCapacityWaitsParams{Ids: ids, Waits: waits, Hosts: hosts}); err != nil {
			return fmt.Errorf("record capacity waits: %w", err)
		}
		if len(requested) > 0 {
			return notifyChannel(ctx, tx, requested[0])
		}
		return nil
	})
	if err != nil {
		return CapacityResult{}, fmt.Errorf("plan capacity: %w", err)
	}
	for _, id := range requested {
		logger.InfoContext(ctx, "host requested", "host_id", id)
	}
	return result, nil
}

// cheapestPerContainer picks the offer whose price per container is lowest
// when r and then as much of later as fits first-fit go onto one new host.
// Offers come best first, so a tie keeps the earlier one.
func cheapestPerContainer(offers []Offer, target Target, r Requirement, later []Requirement) Offer {
	best, bestCost := offers[0], 0.0
	for n, o := range offers {
		host := o.capacity(target)
		host.Reserve(r)
		placed := 1
		for _, other := range later {
			if host.Fits(other) {
				host.Reserve(other)
				placed++
			}
		}
		cost := float64(o.HourlyMicros) / float64(placed)
		if n == 0 || cost < bestCost {
			best, bestCost = o, cost
		}
	}
	return best
}

func pendingDemandOf(ctx context.Context, q *Queries) ([]demandItem, error) {
	rows, err := q.PendingDemand(ctx, demandBatch)
	if err != nil {
		return nil, fmt.Errorf("read pending demand: %w", err)
	}
	out := make([]demandItem, len(rows))
	for n, r := range rows {
		out[n] = demandItem{
			id: r.ID, wait: r.CapacityWait, bought: r.CapacityHostID, boughtPhase: Phase(r.BoughtPhase),
			boughtOffer: [3]string{r.BoughtRegion, r.BoughtType, r.BoughtMarket},
			need: Requirement{
				Workspace: r.WorkspaceID, Connection: r.ConnectionID, Machine: r.Machine, Region: r.Region, Zone: r.Zone,
				Preemptible: r.Preemptible, GPUs: r.Gpus, GPUCount: int(r.GpuCount),
				CPUMillis: r.CpuMillis, MemoryBytes: r.MemoryBytes,
			}}
	}
	return out, nil
}

// size orders requirements for first-fit-decreasing: GPUs dominate, then
// CPU and memory in comparable units.
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

func inFlight(p Phase) bool {
	return p == PhaseRequested || p == PhaseProvisioning || p == PhaseBooting || p == PhaseJoining
}

// coolDown skips the offer bought for d, in this pass and until the
// cooldown ends.
func (p *purchasePlan) coolDown(ctx context.Context, q *Queries, d demandItem) error {
	owner := string(KindPlatform)
	if d.need.Connection != nil {
		owner = d.need.Connection.String()
	}
	region, instanceType, market := d.boughtOffer[0], d.boughtOffer[1], d.boughtOffer[2]
	if instanceType == "" || market == "" {
		return nil
	}
	p.cooled[owner+"/"+region+"/"+instanceType+"/"+market] = true
	if err := q.InsertCooldown(ctx, InsertCooldownParams{
		ConnectionKey: owner, Region: region, InstanceType: instanceType, Market: market,
		Seconds: p.fleet.CapacityCooldown.Seconds(), Reason: "the host bought for a container could not take it",
	}); err != nil {
		return fmt.Errorf("insert cooldown: %w", err)
	}
	return nil
}

// purchasePlan is one pass's purchases.
type purchasePlan struct {
	fleet     Fleet
	networks  map[string]map[string]Network
	live      map[string]int
	cooled    map[string]bool
	opened    []HostCapacity
	openedFor []string
	bought    []InsertRequestedHostParams
}

func (c *Compute) planner(ctx context.Context, q *Queries) (*purchasePlan, error) {
	p := &purchasePlan{
		fleet:    c.fleet,
		networks: map[string]map[string]Network{string(KindPlatform): c.fleet.Networks},
		live:     map[string]int{},
		cooled:   map[string]bool{},
	}
	counts, err := q.LiveCloudHostCounts(ctx)
	if err != nil {
		return nil, fmt.Errorf("count cloud hosts: %w", err)
	}
	for _, row := range counts {
		p.live[row.Owner] = int(row.Hosts)
	}
	cooldowns, err := q.ActiveCooldowns(ctx)
	if err != nil {
		return nil, fmt.Errorf("read cooldowns: %w", err)
	}
	for _, cd := range cooldowns {
		p.cooled[cd.ConnectionKey+"/"+cd.Region+"/"+cd.InstanceType+"/"+cd.Market] = true
	}
	connections, err := q.HostingConnections(ctx)
	if err != nil {
		return nil, fmt.Errorf("read connections: %w", err)
	}
	for _, conn := range connections {
		var networks map[string]Network
		if err := json.Unmarshal(conn.Networks, &networks); err != nil {
			return nil, fmt.Errorf("decode networks of connection %s: %w", conn.ID, err)
		}
		p.networks[conn.ID.String()] = networks
	}
	return p, nil
}

// offerChoices bounds the offers buy compares when it opens a host.
const offerChoices = 12

// buy finds room for r on a host bought in this pass, or opens a host for
// it: of the cheapest offers that take r, the one with the lowest price per
// container once the remaining shortfall is packed onto it. It reports
// WaitLimit when the owner's fleet is full, and no wait when no offer can
// take r.
func (p *purchasePlan) buy(r Requirement, later []Requirement) (CapacityWait, int) {
	target := Target{Kind: KindPlatform}
	if r.Connection != nil {
		target = Target{Kind: KindConnection, Connection: r.Connection}
	}
	owner := target.key()
	for i := range p.opened {
		if p.openedFor[i] == owner && p.opened[i].Fits(r) {
			p.opened[i].Reserve(r)
			return WaitProvisioning, i
		}
	}
	networks, ok := p.networks[owner]
	if !ok {
		return "", -1
	}
	offers := offersFor(r, networks, func(region, instanceType string, market Market) bool {
		return p.cooled[owner+"/"+region+"/"+instanceType+"/"+string(market)]
	})
	if len(offers) == 0 {
		return "", -1
	}
	if p.live[owner] >= p.fleet.MaxHosts {
		return WaitLimit, -1
	}
	o := cheapestPerContainer(offers[:min(len(offers), offerChoices)], target, r, later)
	host := o.capacity(target)
	host.Reserve(r)
	p.opened = append(p.opened, host)
	p.openedFor = append(p.openedFor, owner)
	p.live[owner]++
	cpu, memory := o.usable()
	p.bought = append(p.bought, InsertRequestedHostParams{
		Name: "lazycloud-" + o.Type.Name, Kind: string(target.Kind), ConnectionID: target.Connection,
		CpuMillis: cpu, MemoryBytes: memory, GpuType: o.Type.GPU, GpuCount: int32(o.Type.GPUCount), //nolint:gosec // Catalog GPU counts are small.
		Region: o.Region, AvailabilityZone: o.Zone, AvailabilityZoneID: o.ZoneID, InstanceType: o.Type.Name,
		Market:       ptr(string(o.Market)),
		HourlyMicros: &o.HourlyMicros,
	})
	return WaitProvisioning, len(p.bought) - 1
}
