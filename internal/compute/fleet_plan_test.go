package compute

import (
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"
)

// The plan tests use two shapes: m.small (8 vCPU, 16 GiB) and m.large
// (32 vCPU, 64 GiB, four smalls' usable capacity), in the standard quota
// class like the M family.
var (
	planSmall = CatalogType{Name: "m.small", CPUMillis: 8000, MemoryBytes: 16 * gib, prices: [4]int64{100_000, 100_000, 100_000, 100_000}}
	planLarge = CatalogType{Name: "m.large", CPUMillis: 32_000, MemoryBytes: 64 * gib, prices: [4]int64{300_000, 300_000, 300_000, 300_000}}
	small     = planSmall.Usable(0)
	large     = planLarge.Usable(0)
	onDemand  = ReserveMarket{}
)

// planPolicy keeps warm and stopped floors in the on-demand CPU market and
// nothing anywhere else.
func planPolicy(warm, stopped FleetCapacity) Policy {
	p := DefaultPolicy()
	p.Spot = MarketReserve{}
	p.OnDemand = MarketReserve{Warm: HeadroomTarget{Floor: warm}, Stopped: HeadroomTarget{Floor: stopped}}
	p.GPU = nil
	return p
}

func planSnapshot(t *testing.T, hosts ...FleetHost) FleetSnapshot {
	in := offerInputs(t)
	in.Catalog = []CatalogType{planSmall, planLarge}
	return FleetSnapshot{Now: offerNow, Hosts: hosts, Offers: in, HostRoom: 100, ReserveRoom: 100}
}

func planHost(id byte, typ CatalogType, state FleetState) FleetHost {
	price, _ := typ.OnDemandMicros("us-east-2")
	h := FleetHost{
		ID: HostID{id}, InstanceType: typ.Name, Region: "us-east-2", Zone: "us-east-2a", ZoneID: "use2-az1",
		Market: MarketOnDemand, Usable: typ.Usable(0), State: state, Current: true, Stoppable: true,
		HourlyMicros: ptr(price + rootDiskMicros("us-east-2", rootVolumeGiB) + 5000),
	}
	if h.reserve() {
		h.ReserveMode = ptr(ReserveStop)
	}
	return h
}

// idle makes a serving host idle for an hour.
func idle(h FleetHost) FleetHost {
	h.IdleSince = ptr(offerNow.Add(-time.Hour))
	return h
}

func marketPlan(t *testing.T, plan FleetPlan, m ReserveMarket) MarketPlan {
	t.Helper()
	for _, mp := range plan.Markets {
		if mp.Market == m {
			return mp
		}
	}
	t.Fatalf("no plan for %s", m)
	return MarketPlan{}
}

func actionsOf(plan FleetPlan, kinds ...FleetActionKind) []FleetAction {
	var out []FleetAction
	for _, a := range plan.Actions {
		if slices.Contains(kinds, a.Kind) {
			out = append(out, a)
		}
	}
	return out
}

func boughtTypes(plan FleetPlan) []string {
	var names []string
	for _, a := range actionsOf(plan, ActionBuy) {
		names = append(names, a.Offer.Type.Name)
	}
	return names
}

func hostsOf(actions []FleetAction) []HostID {
	var ids []HostID
	for _, a := range actions {
		ids = append(ids, *a.Host)
	}
	return ids
}

func TestPlanBuysTheLowerTotalCostForTheWarmTarget(t *testing.T) {
	for _, c := range []struct {
		target FleetCapacity
		want   []string
	}{
		{small, []string{"m.small"}},
		{small.Times(4), []string{"m.large"}},
		{large.Times(4), []string{"m.large", "m.large", "m.large", "m.large"}},
	} {
		plan := PlanFleet(planPolicy(c.target, FleetCapacity{}), planSnapshot(t))
		if got := boughtTypes(plan); !slices.Equal(got, c.want) {
			t.Errorf("target %+v bought %v", c.target, got)
		}
		if mp := marketPlan(t, plan, onDemand); !mp.Shortfall.Empty() {
			t.Errorf("target %+v left %+v short", c.target, mp.Shortfall)
		}
	}
}

func TestPlanRefreshesAStaleReserveTheTargetNeedsAndRetiresOneItDoesNot(t *testing.T) {
	stale := planHost(1, planSmall, FleetStopped)
	stale.Current = false
	needed := PlanFleet(planPolicy(FleetCapacity{}, small), planSnapshot(t, stale))
	if got := hostsOf(needed.Actions); len(needed.Actions) != 1 || needed.Actions[0].Kind != ActionRefresh || got[0] != stale.ID {
		t.Fatalf("needed: %+v", needed.Actions)
	}
	surplus := PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), planSnapshot(t, stale))
	if len(surplus.Actions) != 1 || surplus.Actions[0].Kind != ActionRetireReserve {
		t.Fatalf("surplus: %+v", surplus.Actions)
	}
}

func TestPlanPendingCapacityNeverJustifiesRetiringReadyCapacity(t *testing.T) {
	s := planSnapshot(t, idle(planHost(1, planSmall, FleetServing)), planHost(2, planLarge, FleetStarting))
	plan := PlanFleet(planPolicy(small, FleetCapacity{}), s)
	mp := marketPlan(t, plan, onDemand)
	if len(actionsOf(plan, ActionDrain, ActionReturnToReserve)) > 0 || mp.WarmFree != small || mp.WarmPending != large {
		t.Fatalf("actions %+v free %+v pending %+v", plan.Actions, mp.WarmFree, mp.WarmPending)
	}
}

func TestPlanRetiresAPendingReserveBeforeTheReadyOneItWouldReplace(t *testing.T) {
	ready := planHost(1, planSmall, FleetStopped)
	pending := planHost(2, planSmall, FleetPreparing)
	plan := PlanFleet(planPolicy(FleetCapacity{}, small), planSnapshot(t, ready, pending))
	mp := marketPlan(t, plan, onDemand)
	if got := hostsOf(actionsOf(plan, ActionRetireReserve)); !slices.Equal(got, []HostID{{2}}) || mp.ReserveReady != small {
		t.Fatalf("retired %v ready %+v", got, mp.ReserveReady)
	}
}

func TestPlanAResumedReserveCannotCoverRetiringTheLastReadyOne(t *testing.T) {
	cheap := planHost(1, planSmall, FleetStopped)
	cheap.HourlyMicros = ptr(int64(10))
	preparing := planHost(2, planSmall, FleetPreparing)
	keep := planHost(3, planSmall, FleetStopped)
	keep.HourlyMicros = ptr(int64(20))
	plan := PlanFleet(planPolicy(small, small), planSnapshot(t, cheap, preparing, keep))
	mp := marketPlan(t, plan, onDemand)
	if got := hostsOf(actionsOf(plan, ActionResume)); !slices.Equal(got, []HostID{{1}}) {
		t.Fatalf("resumed %v", got)
	}
	if retired := hostsOf(actionsOf(plan, ActionRetireReserve)); slices.Contains(retired, HostID{3}) || mp.ReserveReady != small {
		t.Fatalf("retired %v ready %+v", retired, mp.ReserveReady)
	}
}

func TestSpotWorkBorrowsAnOnDemandReserveOnlyAboveItsTarget(t *testing.T) {
	for _, c := range []struct {
		reserves int
		target   FleetCapacity
		borrows  bool
	}{
		{1, small, false},
		{3, small.Times(3), false},
		{3, small.Times(2), true},
	} {
		var hosts []FleetHost
		for i := range c.reserves {
			hosts = append(hosts, planHost(byte(i+1), planSmall, FleetStopped))
		}
		s := planSnapshot(t, hosts...)
		s.Pending = []DemandGroup{{Need: Requirement{Preemptible: true, CPUMillis: 4000, MemoryBytes: 4 * gib}, Containers: []PendingContainer{{ID: uuid.New()}}}}
		plan := PlanFleet(planPolicy(FleetCapacity{}, c.target), s)
		if borrowed := len(actionsOf(plan, ActionResume)) == 1; borrowed != c.borrows {
			t.Errorf("%d reserves, target %+v: actions %+v", c.reserves, c.target, plan.Actions)
		}
	}
}

func pendingOne(need Requirement, host *HostID) (DemandGroup, uuid.UUID) {
	id := uuid.New()
	return DemandGroup{Need: need, Containers: []PendingContainer{{ID: id, Host: host}}}, id
}

func waitOf(t *testing.T, plan FleetPlan, id uuid.UUID) ContainerWait {
	t.Helper()
	for _, w := range plan.Waits {
		if w.Container == id {
			return w
		}
	}
	t.Fatalf("no wait for %s", id)
	return ContainerWait{}
}

func TestPlanPlacesDemandOnReadyRoomThenStartingHostsThenReservesThenPurchases(t *testing.T) {
	need := Requirement{CPUMillis: 4000, MemoryBytes: 4 * gib}
	p := planPolicy(FleetCapacity{}, FleetCapacity{})

	s := planSnapshot(t, planHost(1, planSmall, FleetServing))
	g, id := pendingOne(need, nil)
	s.Pending = []DemandGroup{g}
	if plan := PlanFleet(p, s); len(plan.Actions) > 0 || waitOf(t, plan, id).Wait != nil {
		t.Fatalf("ready room: %+v %+v", plan.Actions, waitOf(t, plan, id))
	}

	s = planSnapshot(t, planHost(1, planSmall, FleetStarting), planHost(2, planSmall, FleetStarting))
	g, id = pendingOne(need, ptr(HostID{2}))
	s.Pending = []DemandGroup{g}
	if w := waitOf(t, PlanFleet(p, s), id); *w.Wait != WaitProvisioning || *w.Host != (HostID{2}) {
		t.Fatalf("the host bought for it: %+v", w)
	}

	s = planSnapshot(t, planHost(1, planSmall, FleetStopped))
	g, id = pendingOne(need, nil)
	s.Pending = []DemandGroup{g}
	plan := PlanFleet(p, s)
	if w := waitOf(t, plan, id); len(plan.Actions) != 1 || plan.Actions[0].Kind != ActionResume ||
		!slices.Equal(plan.Actions[0].Containers, []uuid.UUID{id}) || *w.Host != (HostID{1}) {
		t.Fatalf("reserve: %+v %+v", plan.Actions, w)
	}

	s = planSnapshot(t)
	g, id = pendingOne(need, nil)
	s.Pending = []DemandGroup{g}
	plan = PlanFleet(p, s)
	if w := waitOf(t, plan, id); len(plan.Actions) != 1 || plan.Actions[0].Kind != ActionBuy || *w.Wait != WaitProvisioning || *w.Action != 0 {
		t.Fatalf("purchase: %+v %+v", plan.Actions, w)
	}

	s.HostRoom = 0
	plan = PlanFleet(p, s)
	if w := waitOf(t, plan, id); len(plan.Actions) > 0 || *w.Wait != WaitLimit || marketPlan(t, plan, onDemand).Reason != ReasonDemand {
		t.Fatalf("fleet limit: %+v %+v", plan.Actions, w)
	}
}

func TestPlanCoversABatchOfContainersTogether(t *testing.T) {
	s := planSnapshot(t)
	s.Offers.Catalog = FleetCatalog()
	need := Requirement{CPUMillis: 6000, MemoryBytes: 4 * gib}
	s.Pending = []DemandGroup{{Need: need, Containers: []PendingContainer{{ID: uuid.New()}, {ID: uuid.New()}}}}
	plan := PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), s)
	if got := boughtTypes(plan); !slices.Equal(got, []string{"c6a.4xlarge"}) || len(plan.Actions[0].Containers) != 2 {
		t.Fatalf("two 6 vCPU containers: %+v", plan.Actions)
	}
}

func TestPlanCapsGrowthPerMarketAndPass(t *testing.T) {
	plan := PlanFleet(planPolicy(large.Times(20), FleetCapacity{}), planSnapshot(t))
	mp := marketPlan(t, plan, onDemand)
	if n := len(actionsOf(plan, ActionBuy)); n != 16 || mp.Reason != ReasonActionCap || mp.Shortfall.Empty() {
		t.Fatalf("%d buys, reason %q, short %+v", n, mp.Reason, mp.Shortfall)
	}
}

// A reserve runs while it is prepared, so buying one takes host room: a
// full fleet buys none, however short the reserve.
func TestPlanBuysReservesOnlyWithinTheHostLimit(t *testing.T) {
	for _, c := range []struct {
		room, want int
	}{{0, 0}, {100, 1}} {
		s := planSnapshot(t)
		s.HostRoom = c.room
		plan := PlanFleet(planPolicy(FleetCapacity{}, small.Times(2)), s)
		if n := len(actionsOf(plan, ActionBuyReserve)); n != c.want {
			t.Errorf("host room %d: %d reserves bought, want %d", c.room, n, c.want)
		}
	}
}

func TestAnyGPUDemandGoesToAReservedCardTheFleetHoldsThenTheCheapestPerCard(t *testing.T) {
	need := Requirement{GPUs: []string{GPUAny}, GPUCount: 1, CPUMillis: 2000, MemoryBytes: 8 * gib}
	p := planPolicy(FleetCapacity{}, FleetCapacity{})
	p.GPU = DefaultPolicy().GPU
	loaded := func(typ string, model string) FleetHost {
		h := planHost(1, mustType(t, typ), FleetServing)
		h.GPU, h.Load = model, h.Usable
		return h
	}
	for _, c := range []struct {
		name  string
		hosts []FleetHost
		want  string
	}{
		{"no stock", nil, "g4dn.xlarge"},
		{"L4 in stock", []FleetHost{loaded("g6.2xlarge", "L4")}, "g6.2xlarge"},
		{"L40S is not a reserved card", []FleetHost{loaded("g6e.4xlarge", "L40S")}, "g4dn.xlarge"},
	} {
		s := planSnapshot(t, c.hosts...)
		s.Offers.Catalog = FleetCatalog()
		g, _ := pendingOne(need, nil)
		s.Pending = []DemandGroup{g}
		plan := PlanFleet(p, s)
		if got := boughtTypes(plan); !slices.Equal(got, []string{c.want}) {
			t.Errorf("%s: bought %v", c.name, got)
		}
	}
}

func TestGPUWorkFallsThroughToTheModelItListsNext(t *testing.T) {
	s := planSnapshot(t)
	s.Offers.Catalog = FleetCatalog()
	g, _ := pendingOne(Requirement{GPUs: []string{"H100", "L4"}, GPUCount: 1, CPUMillis: 2000, MemoryBytes: 8 * gib}, nil)
	s.Pending = []DemandGroup{g}
	plan := PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), s)
	// Both H100 types fail the margin, so the L4 serves.
	if buys := actionsOf(plan, ActionBuy); len(buys) != 1 || buys[0].Offer.Type.GPU != "L4" || buys[0].Market != (ReserveMarket{GPU: "L4"}) {
		t.Fatalf("actions %+v", plan.Actions)
	}
}

func TestPlanDrainsAOneTimeSpotHostInsteadOfStoppingIt(t *testing.T) {
	p := planPolicy(FleetCapacity{}, FleetCapacity{})
	p.Spot = MarketReserve{Stopped: HeadroomTarget{Floor: small}}
	h := idle(planHost(1, planSmall, FleetServing))
	h.Market = MarketSpot
	for _, stoppable := range []bool{false, true} {
		h.Stoppable = stoppable
		plan := PlanFleet(p, planSnapshot(t, h))
		if returned := len(actionsOf(plan, ActionReturnToReserve)) == 1; returned != stoppable || (!stoppable && len(actionsOf(plan, ActionDrain)) != 1) {
			t.Errorf("stoppable %v: %+v", stoppable, plan.Actions)
		}
	}
}

// planFast is a small shape that hibernates; planRoomy can hibernate but
// has more RAM than a reserve hibernates.
var (
	planFast  = CatalogType{Name: "fast", CPUMillis: 8000, MemoryBytes: 16 * gib, Hibernates: true, prices: [4]int64{100_000, 100_000, 100_000, 100_000}}
	planRoomy = CatalogType{Name: "roomy", CPUMillis: 8000, MemoryBytes: 64 * gib, Hibernates: true, prices: [4]int64{200_000, 200_000, 200_000, 200_000}}
)

func TestPlanKeepsHeadroomGrowthOutOfDemand(t *testing.T) {
	s := planSnapshot(t)
	g, _ := pendingOne(Requirement{CPUMillis: 1000, MemoryBytes: gib}, nil)
	s.Pending = []DemandGroup{g}
	plan := PlanFleet(planPolicy(large, small), s)
	for _, a := range plan.Actions {
		if len(a.Containers) == 0 {
			t.Errorf("headroom %s while work waits", a.Kind)
		}
	}
	if mp := marketPlan(t, plan, onDemand); mp.Reason != ReasonDemand {
		t.Errorf("reason %q", mp.Reason)
	}
}

// An idle host leaves once idle for the idle timeout; a host pending work
// fits is not idle.
func TestPlanReleasesIdleHostsAfterTheIdleTimeout(t *testing.T) {
	fresh := planHost(1, planSmall, FleetServing)
	fresh.IdleSince = ptr(offerNow.Add(-4 * time.Minute))
	old := idle(planHost(2, planSmall, FleetServing))
	wanted := idle(planHost(3, planLarge, FleetServing))
	s := planSnapshot(t, fresh, old, wanted)
	g, _ := pendingOne(Requirement{CPUMillis: 16_000, MemoryBytes: 32 * gib}, nil)
	s.Pending = []DemandGroup{g}
	plan := PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), s)
	if got := hostsOf(actionsOf(plan, ActionDrain)); !slices.Equal(got, []HostID{{2}}) || len(plan.Actions) != 1 {
		t.Fatalf("actions %+v", plan.Actions)
	}
	if since, ok := plan.IdleSince[fresh.ID]; !ok || !since.Equal(*fresh.IdleSince) {
		t.Fatalf("idle since %v", since)
	}
	if _, ok := plan.IdleSince[wanted.ID]; ok {
		t.Fatal("a host pending work fits is idle")
	}
}

// Pending work that fits ready room takes it, so that room is no longer
// headroom: an idle host the warm target needs once the work lands stays.
func TestPlanCountsRoomPendingWorkTakesAgainstTheWarmTarget(t *testing.T) {
	need := Requirement{CPUMillis: 6000, MemoryBytes: 8 * gib}
	s := planSnapshot(t, planHost(1, planSmall, FleetServing), idle(planHost(2, planSmall, FleetServing)))
	g, _ := pendingOne(need, nil)
	s.Pending = []DemandGroup{g}
	plan := PlanFleet(planPolicy(cpuGiB(4000, 8), FleetCapacity{}), s)
	if mp := marketPlan(t, plan, onDemand); len(plan.Actions) > 0 || mp.WarmFree != small.Times(2).Minus(reservedShape(need)) {
		t.Fatalf("actions %+v, warm free %+v", plan.Actions, mp.WarmFree)
	}
}

func TestPlanReleasesTheCostliestIdleHostFirst(t *testing.T) {
	cheap := idle(planHost(1, planSmall, FleetServing))
	costly := idle(planHost(2, planSmall, FleetServing))
	costly.HourlyMicros = ptr(int64(500_000))
	plan := PlanFleet(planPolicy(small, FleetCapacity{}), planSnapshot(t, cheap, costly))
	if got := hostsOf(plan.Actions); !slices.Equal(got, []HostID{{2}}) || plan.Actions[0].Kind != ActionDrain {
		t.Fatalf("actions %+v", plan.Actions)
	}
}

// At zero load the warm floor converges on the cheapest host that meets it:
// an idle c6a.2xlarge ($0.32/h) is replaced by an m7i.large ($0.12/h) and
// leaves once the replacement serves. A host running work is not replaced.
func TestPlanWarmFloorConvergesOnTheCheapestHost(t *testing.T) {
	p := DefaultPolicy()
	p.Spot, p.GPU, p.OnDemand = MarketReserve{}, nil, MarketReserve{Warm: p.OnDemand.Warm}
	snapshot := func(hosts ...FleetHost) FleetSnapshot {
		s := planSnapshot(t, hosts...)
		s.Offers.Catalog = FleetCatalog()
		return s
	}
	big := idle(planHost(1, mustType(t, "c6a.2xlarge"), FleetServing))
	plan := PlanFleet(p, snapshot(big))
	if moves := actionsOf(plan, ActionRightsize); len(plan.Actions) != 1 || len(moves) != 1 || *moves[0].Host != big.ID ||
		moves[0].Offer.Type.Name != "m7i.large" {
		t.Fatalf("idle c6a.2xlarge: %+v", plan.Actions)
	}
	if plan := PlanFleet(p, snapshot(big, planHost(2, mustType(t, "m7i.large"), FleetStarting))); len(plan.Actions) > 0 {
		t.Fatalf("while the replacement starts: %+v", plan.Actions)
	}
	plan = PlanFleet(p, snapshot(big, idle(planHost(2, mustType(t, "m7i.large"), FleetServing))))
	if got := hostsOf(plan.Actions); !slices.Equal(got, []HostID{{1}}) || plan.Actions[0].Kind != ActionDrain {
		t.Fatalf("once it serves: %+v", plan.Actions)
	}
	busy := planHost(1, mustType(t, "c6a.2xlarge"), FleetServing)
	busy.Load, busy.Containers = cpuGiB(1000, 1), 1
	if plan := PlanFleet(p, snapshot(busy)); len(plan.Actions) > 0 {
		t.Fatalf("a serving host was replaced: %+v", plan.Actions)
	}
}

// An idle host goes back to the reserve while the reserve is short: an
// on-demand host of at most 32 GiB launched able to hibernates, any other
// stops plainly. Once the reserve is held, it drains.
func TestPlanReturnsALeavingHostToTheReserveWhileTheReserveFallsShort(t *testing.T) {
	p := planPolicy(FleetCapacity{}, small)
	p.Spot = MarketReserve{Stopped: HeadroomTarget{Floor: small}}
	hibernating := func(typ CatalogType, market Market) FleetHost {
		h := idle(planHost(1, typ, FleetServing))
		h.Market, h.HibernationConfigured = market, true
		return h
	}
	for _, c := range []struct {
		name string
		host FleetHost
		mode ReserveMode
	}{
		{"plain", idle(planHost(1, planSmall, FleetServing)), ReserveStop},
		{"on-demand, hibernation configured", hibernating(planFast, MarketOnDemand), ReserveHibernate},
		{"on-demand, 64 GiB", hibernating(planRoomy, MarketOnDemand), ReserveStop},
		{"Spot, hibernation configured", hibernating(planFast, MarketSpot), ReserveStop},
	} {
		s := planSnapshot(t, c.host)
		s.Offers.Catalog = append(s.Offers.Catalog, planFast, planRoomy)
		plan := PlanFleet(p, s)
		if got := actionsOf(plan, ActionReturnToReserve); len(got) != 1 || *got[0].Mode != c.mode || len(actionsOf(plan, ActionDrain)) > 0 {
			t.Errorf("%s: %+v", c.name, plan.Actions)
		}
	}
	s := planSnapshot(t, idle(planHost(1, planSmall, FleetServing)), planHost(2, planSmall, FleetStopped))
	if plan := PlanFleet(p, s); !slices.Equal(hostsOf(plan.Actions), []HostID{{1}}) || plan.Actions[0].Kind != ActionDrain {
		t.Fatalf("reserve already held: %+v", plan.Actions)
	}
}
