package compute

import (
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"
)

// The plan tests use two shapes: m.small (8 CPU, 16 GiB) and m.large
// (32 CPU, 64 GiB, four smalls' usable capacity), in the standard quota
// class like the M family.
var (
	planSmall = CatalogType{Name: "m.small", Topology: twoPerCore(16), MemoryBytes: 16 * gib, prices: [4]int64{100_000, 100_000, 100_000, 100_000}}
	planLarge = CatalogType{Name: "m.large", Topology: twoPerCore(64), MemoryBytes: 64 * gib, prices: [4]int64{300_000, 300_000, 300_000, 300_000}}
	small     = planSmall.Usable(0)
	large     = planLarge.Usable(0)
	onDemand  = ReserveMarket{}
)

// planPolicy keeps a running floor and a stopped floor in the on-demand
// CPU market and nothing anywhere else.
func planPolicy(floor, stopped FleetCapacity) Policy {
	p := DefaultPolicy()
	p.Spot = MarketReserve{}
	p.OnDemand = MarketReserve{Floor: floor, Stopped: HeadroomTarget{Floor: stopped}}
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
		HourlyMicros: ptr(price + rootDiskMicros("us-east-2", rootVolumeGiB, baselineMiBps) + 5000),
	}
	if h.reserve() {
		h.ReserveMode = ptr(ReserveStop)
	}
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

// boughtForWork are the types bought for pending containers.
func boughtForWork(plan FleetPlan) []string {
	var names []string
	for _, a := range actionsOf(plan, ActionBuy) {
		if len(a.Containers) > 0 {
			names = append(names, a.Offer.Type.Name)
		}
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

func pendingOne(need Requirement, host *HostID) (DemandGroup, uuid.UUID) {
	id := uuid.New()
	return DemandGroup{Need: need, Containers: []PendingContainer{{ID: id, Since: offerNow, Host: host}}}, id
}

func gpuPolicy() Policy {
	p := planPolicy(FleetCapacity{}, FleetCapacity{})
	p.GPU = DefaultPolicy().GPU
	return p
}

func spotCooled(model string) []OfferCooldown {
	var out []OfferCooldown
	for _, typ := range FleetCatalog() {
		if typ.GPU == model {
			out = append(out, OfferCooldown{Region: "us-east-2", InstanceType: typ.Name, Market: MarketSpot, Until: offerNow.Add(time.Hour)})
		}
	}
	return out
}

func gpuReserve(t *testing.T, id byte, market Market) FleetHost {
	h := planHost(id, mustType(t, "g6.xlarge"), FleetStopped)
	h.Market, h.GPU = market, "L4"
	return h
}

func gpuPlanSnapshot(t *testing.T, hosts ...FleetHost) FleetSnapshot {
	s := planSnapshot(t, hosts...)
	s.Offers.Catalog = FleetCatalog()
	for typ, micros := range map[string]int64{"g6.xlarge": 300_000, "g6.4xlarge": 500_000, "g5.2xlarge": 500_000} {
		s.Offers.Spot = append(s.Offers.Spot, SpotQuote{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: typ, HourlyMicros: micros, ObservedAt: offerNow})
	}
	return s
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

func TestPlanReplacesAStaleReserveBeforeRetiringIt(t *testing.T) {
	stale := planHost(1, planSmall, FleetStopped)
	stale.Current = false
	kinds := func(plan FleetPlan) []FleetActionKind {
		var out []FleetActionKind
		for _, a := range plan.Actions {
			out = append(out, a.Kind)
		}
		return out
	}
	if got := kinds(PlanFleet(planPolicy(FleetCapacity{}, small), planSnapshot(t, stale))); !slices.Equal(got, []FleetActionKind{ActionBuyReserve}) {
		t.Fatalf("needed: %v", got)
	}
	replaced := planHost(2, planSmall, FleetStopped)
	if got := kinds(PlanFleet(planPolicy(FleetCapacity{}, small), planSnapshot(t, stale, replaced))); !slices.Equal(got, []FleetActionKind{ActionRetireReserve}) {
		t.Fatalf("replaced: %v", got)
	}
	snapshot := planSnapshot(t, stale)
	snapshot.Pending = []DemandGroup{{Need: Requirement{CPUMillis: 1000, MemoryBytes: gib}, Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow}}}}
	if got := kinds(PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), snapshot)); slices.Contains(got, ActionResume) {
		t.Fatalf("CPU work resumed a stale reserve: %v", got)
	}
}

func TestPendingWorkBuysPastAHostDueAnAgentUpdate(t *testing.T) {
	buys := func(due bool) int {
		h := planHost(1, planSmall, FleetServing)
		h.UpdateDue = due
		snapshot := planSnapshot(t, h)
		snapshot.Pending = []DemandGroup{{Need: Requirement{CPUMillis: 1000, MemoryBytes: gib}, Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow}}}}
		n := 0
		for _, a := range PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), snapshot).Actions {
			if a.Kind == ActionBuy {
				n++
			}
		}
		return n
	}
	if n := buys(false); n != 0 {
		t.Fatalf("bought %d hosts for work a serving host fits", n)
	}
	if n := buys(true); n != 1 {
		t.Fatalf("bought %d hosts for work only a host due an update fits, want 1", n)
	}
}

func TestPlanRetiresAnInterruptedReserveAndBuysItsPlace(t *testing.T) {
	interrupted := planHost(1, planSmall, FleetStopped)
	interrupted.Protected = true
	plan := PlanFleet(planPolicy(FleetCapacity{}, small), planSnapshot(t, interrupted))
	if got := hostsOf(actionsOf(plan, ActionRetireReserve)); !slices.Equal(got, []HostID{{1}}) || len(actionsOf(plan, ActionBuyReserve)) != 1 {
		t.Fatalf("actions %+v", plan.Actions)
	}
}

func TestPlanPendingCapacityNeverJustifiesRetiringReadyCapacity(t *testing.T) {
	s := planSnapshot(t, planHost(1, planSmall, FleetServing), planHost(2, planLarge, FleetStarting))
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
		s.Pending = []DemandGroup{{Need: Requirement{Preemptible: true, CPUMillis: 4000, MemoryBytes: 4 * gib}, Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow}}}}
		plan := PlanFleet(planPolicy(FleetCapacity{}, c.target), s)
		if borrowed := len(actionsOf(plan, ActionResume)) == 1; borrowed != c.borrows {
			t.Errorf("%d reserves, target %+v: actions %+v", c.reserves, c.target, plan.Actions)
		}
	}
}

func TestOnDemandHostsHandSpotWorkBackWhileSpotCanHostIt(t *testing.T) {
	quoted := func(s FleetSnapshot) FleetSnapshot {
		s.Offers.Spot = append(s.Offers.Spot, SpotQuote{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: planSmall.Name, HourlyMicros: 30_000, ObservedAt: offerNow})
		return s
	}
	moves := []Requirement{{Preemptible: true, CPUMillis: 2000, MemoryBytes: 2 * gib}}
	moving := planHost(1, planSmall, FleetServing)
	moving.Load, moving.Lent, moving.Moves, moving.Containers = cpuGiB(2000, 2), cpuGiB(2000, 2), moves, 1
	kept := planHost(2, planSmall, FleetServing)
	kept.Load, kept.Lent, kept.Moves, kept.Stays, kept.Containers = cpuGiB(4000, 4), cpuGiB(2000, 2), moves, true, 2
	empty := planHost(3, planSmall, FleetServing)
	p := planPolicy(FleetCapacity{}, FleetCapacity{})

	s := quoted(planSnapshot(t, moving, kept, empty))
	need, waiting := pendingOne(Requirement{Preemptible: true, CPUMillis: 1000, MemoryBytes: gib}, nil)
	s.Pending = []DemandGroup{need}
	plan := PlanFleet(p, s)
	if back := actionsOf(plan, ActionHandBack); !slices.Equal(hostsOf(back), []HostID{moving.ID}) || len(actionsOf(plan, ActionLend)) > 0 {
		t.Fatalf("with Spot quoted: %+v", plan.Actions)
	}
	if w := waitOf(t, plan, waiting); w.Wait != nil {
		t.Fatalf("Spot-tolerant work waits %+v, want the room the kept host lends", w)
	}

	cooling := quoted(planSnapshot(t, moving))
	cooling.Offers.Cooldowns = []OfferCooldown{{Region: "us-east-2", ZoneID: "use2-az2", InstanceType: planLarge.Name, Market: MarketSpot, Until: offerNow.Add(time.Minute)}}
	if plan := PlanFleet(p, cooling); len(actionsOf(plan, ActionHandBack)) > 0 {
		t.Fatalf("while a Spot pool of the region cools: %+v", plan.Actions)
	}

	moving.HandsBack = true
	if plan := PlanFleet(p, cooling); len(plan.Actions) > 0 {
		t.Fatalf("handing back while Spot still hosts the work: %+v", plan.Actions)
	}
	if plan := PlanFleet(p, planSnapshot(t, moving, kept)); !slices.Equal(hostsOf(actionsOf(plan, ActionLend)), []HostID{moving.ID}) {
		t.Fatalf("once no Spot offer is left: %+v", plan.Actions)
	}
	kept.HandsBack = true
	if plan := PlanFleet(p, quoted(planSnapshot(t, kept))); !slices.Equal(hostsOf(actionsOf(plan, ActionLend)), []HostID{kept.ID}) {
		t.Fatalf("a host handing back that runs work that cannot move: %+v", plan.Actions)
	}
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
	if w := waitOf(t, plan, id); len(plan.Actions) > 0 || *w.Wait != WaitLimit || marketPlan(t, plan, onDemand).Reason != WaitLimit {
		t.Fatalf("fleet limit: %+v %+v", plan.Actions, w)
	}
}

func TestPlanCoversABatchOfContainersTogether(t *testing.T) {
	s := planSnapshot(t)
	s.Offers.Catalog = FleetCatalog()
	need := Requirement{CPUMillis: 3000, MemoryBytes: 4 * gib}
	s.Pending = []DemandGroup{{Need: need, Containers: []PendingContainer{{ID: uuid.New()}, {ID: uuid.New()}}}}
	plan := PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), s)
	if got := boughtTypes(plan); !slices.Equal(got, []string{"c6a.4xlarge"}) || len(plan.Actions[0].Containers) != 2 {
		t.Fatalf("two 3 CPU containers: %+v", plan.Actions)
	}
}

func TestPlanCapsGrowthPerMarketAndPass(t *testing.T) {
	s := planSnapshot(t)
	group := DemandGroup{Need: Requirement{CPUMillis: large.CPUMillis, MemoryBytes: large.MemoryBytes}}
	for range 20 {
		group.Containers = append(group.Containers, PendingContainer{ID: uuid.New(), Since: offerNow})
	}
	s.Pending = []DemandGroup{group}
	plan := PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), s)
	deferred := 0
	for _, w := range plan.Waits {
		if w.Wait != nil && *w.Wait == WaitDeferred {
			deferred++
		}
	}
	if n := len(actionsOf(plan, ActionBuy)); n != 16 || deferred != 4 {
		t.Fatalf("%d buys, %d containers deferred; want 16 and the 4 left to the next pass", n, deferred)
	}
}

func TestPlanBuysReservesOnlyWithinTheHostLimit(t *testing.T) {
	for _, room := range []int{0, 100} {
		s := planSnapshot(t)
		s.HostRoom = room
		plan := PlanFleet(planPolicy(FleetCapacity{}, small.Times(2)), s)
		if n := len(actionsOf(plan, ActionBuyReserve)); (n > 0) != (room > 0) {
			t.Errorf("host room %d: %d reserves bought", room, n)
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
		if got := boughtForWork(plan); !slices.Equal(got, []string{c.want}) {
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
	h := planHost(1, planSmall, FleetServing)
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
	planFast  = CatalogType{Name: "fast", Topology: twoPerCore(16), MemoryBytes: 16 * gib, Hibernates: true, prices: [4]int64{100_000, 100_000, 100_000, 100_000}}
	planRoomy = CatalogType{Name: "roomy", Topology: twoPerCore(16), MemoryBytes: 64 * gib, Hibernates: true, prices: [4]int64{200_000, 200_000, 200_000, 200_000}}
)

func TestPlanReleasesTheCostliestIdleHostFirst(t *testing.T) {
	dear := planSmall
	dear.Name, dear.prices = "m.dear", [4]int64{500_000, 500_000, 500_000, 500_000}
	costly := planHost(1, dear, FleetServing)
	cheap := planHost(2, planSmall, FleetServing)
	s := planSnapshot(t, costly, cheap)
	s.Offers.Catalog = append(s.Offers.Catalog, dear)
	plan := PlanFleet(planPolicy(small, FleetCapacity{}), s)
	if got := hostsOf(plan.Actions); !slices.Equal(got, []HostID{{1}}) || plan.Actions[0].Kind != ActionDrain {
		t.Fatalf("actions %+v", plan.Actions)
	}
}

func TestPlanWarmFloorConvergesOnTheCheapestHost(t *testing.T) {
	p := DefaultPolicy()
	p.Spot, p.GPU, p.OnDemand = MarketReserve{}, nil, MarketReserve{Floor: p.OnDemand.Floor}
	snapshot := func(hosts ...FleetHost) FleetSnapshot {
		s := planSnapshot(t, hosts...)
		s.Offers.Catalog = FleetCatalog()
		return s
	}
	big := planHost(1, mustType(t, "c6a.2xlarge"), FleetServing)
	plan := PlanFleet(p, snapshot(big))
	if moves := actionsOf(plan, ActionRightsize); len(plan.Actions) != 1 || len(moves) != 1 || *moves[0].Host != big.ID ||
		moves[0].Offer.Type.Name != "m7i.large" {
		t.Fatalf("idle c6a.2xlarge: %+v", plan.Actions)
	}
	if plan := PlanFleet(p, snapshot(big, planHost(2, mustType(t, "m7i.large"), FleetStarting))); len(plan.Actions) > 0 {
		t.Fatalf("while the replacement starts: %+v", plan.Actions)
	}
	replacement := planHost(2, mustType(t, "m7i.large"), FleetServing)
	replacement.Replaces = &big.ID
	plan = PlanFleet(p, snapshot(big, replacement))
	if got := hostsOf(plan.Actions); !slices.Equal(got, []HostID{{1}}) || plan.Actions[0].Kind != ActionDrain {
		t.Fatalf("once it serves: %+v", plan.Actions)
	}
	replacement.Load, replacement.Containers = cpuGiB(1000, 4), 1
	if plan := PlanFleet(p, snapshot(big, replacement)); len(actionsOf(plan, ActionDrain)) > 0 {
		t.Fatalf("work took the replacement's floor and the replaced host left: %+v", plan.Actions)
	}
	busy := planHost(1, mustType(t, "c6a.2xlarge"), FleetServing)
	busy.Load, busy.Containers = cpuGiB(1000, 1), 1
	if plan := PlanFleet(p, snapshot(busy)); len(plan.Actions) > 0 {
		t.Fatalf("a serving host was replaced: %+v", plan.Actions)
	}
}

func TestPlanReturnsALeavingHostToTheReserveWhileTheReserveFallsShort(t *testing.T) {
	p := planPolicy(FleetCapacity{}, small)
	p.Spot = MarketReserve{Stopped: HeadroomTarget{Floor: small}}
	hibernating := func(typ CatalogType, market Market) FleetHost {
		h := planHost(1, typ, FleetServing)
		h.Market, h.HibernationConfigured = market, true
		return h
	}
	for _, c := range []struct {
		name string
		host FleetHost
		mode ReserveMode
	}{
		{"plain", planHost(1, planSmall, FleetServing), ReserveStop},
		{"on-demand, hibernation configured", hibernating(planFast, MarketOnDemand), ReserveHibernate},
		{"on-demand, 64 GiB", hibernating(planRoomy, MarketOnDemand), ReserveStop},
		{"Spot, hibernation configured", hibernating(planFast, MarketSpot), ReserveHibernate},
	} {
		s := planSnapshot(t, c.host)
		s.Offers.Catalog = append(s.Offers.Catalog, planFast, planRoomy)
		plan := PlanFleet(p, s)
		if got := actionsOf(plan, ActionReturnToReserve); len(got) != 1 || *got[0].Mode != c.mode || len(actionsOf(plan, ActionDrain)) > 0 {
			t.Errorf("%s: %+v", c.name, plan.Actions)
		}
	}
	s := planSnapshot(t, planHost(1, planSmall, FleetServing), planHost(2, planSmall, FleetStopped))
	if plan := PlanFleet(p, s); !slices.Equal(hostsOf(plan.Actions), []HostID{{1}}) || plan.Actions[0].Kind != ActionDrain {
		t.Fatalf("reserve already held: %+v", plan.Actions)
	}
}

func TestOneRunningCardKeepsAStoppedTargetOfOneCard(t *testing.T) {
	p := planPolicy(FleetCapacity{}, FleetCapacity{})
	p.GPU = DefaultPolicy().GPU
	h := planHost(1, mustType(t, "g4dn.xlarge"), FleetServing)
	h.GPU, h.Load, h.Containers = "T4", FleetCapacity{CPUMillis: 2000, MemoryBytes: 8 * gib, GPUs: 1}, 1
	s := planSnapshot(t, h)
	s.Offers.Catalog = FleetCatalog()
	s.Recent = map[ReserveMarket]FleetCapacity{{GPU: "T4"}: h.Load}
	if target := marketPlan(t, PlanFleet(p, s), ReserveMarket{GPU: "T4"}).StoppedTarget; target.GPUs != 1 {
		t.Fatalf("T4 stopped target %+v, want one card", target)
	}
}

func TestBorrowedOnDemandRoomCountsAsSpotLoad(t *testing.T) {
	p := DefaultPolicy()
	p.GPU = nil
	h := planHost(1, mustType(t, "c6a.4xlarge"), FleetServing)
	h.Load, h.Lent, h.Containers = cpuGiB(6000, 12), cpuGiB(4000, 8), 3
	plan := PlanFleet(p, planSnapshot(t, h))
	if spot := marketPlan(t, plan, ReserveMarket{Preemptible: true}).Load; spot != cpuGiB(4000, 8) {
		t.Errorf("Spot load %+v, want the borrowed 4 CPU", spot)
	}
	if od := marketPlan(t, plan, onDemand).Load; od != cpuGiB(2000, 4) {
		t.Errorf("on-demand load %+v, want the 2 CPU that cannot run on Spot", od)
	}
}

func TestALargeWarmHostDoesNotStandInForTheReserve(t *testing.T) {
	p := DefaultPolicy()
	p.Spot, p.GPU = MarketReserve{}, nil
	big := mustType(t, "c6a.8xlarge")
	p.OnDemand = MarketReserve{Floor: cpuGiB(1000, 4), Stopped: HeadroomTarget{Floor: cpuGiB(1000, 4)}, FitLargest: true}
	warm := planHost(1, big, FleetServing)
	warm.RightsizeRefusedAt = ptr(offerNow)
	s := planSnapshot(t, warm)
	s.Offers.Catalog = FleetCatalog()
	s.Recent = map[ReserveMarket]FleetCapacity{onDemand: cpuGiB(12000, 24)}
	plan := PlanFleet(p, s)
	if len(actionsOf(plan, ActionBuyReserve)) == 0 {
		t.Fatalf("actions %+v, reason %q; want a reserve bought", plan.Actions, marketPlan(t, plan, onDemand).Reason)
	}
}

func TestPurchasesWaitForArrivalsToSettleButResumesDoNot(t *testing.T) {
	need := Requirement{CPUMillis: 4000, MemoryBytes: 4 * gib}
	p := planPolicy(FleetCapacity{}, FleetCapacity{})
	batching := func(hosts ...FleetHost) (FleetSnapshot, uuid.UUID) {
		s := planSnapshot(t, hosts...)
		s.BatchWait = 800 * time.Millisecond
		g, id := pendingOne(need, nil)
		s.Pending = []DemandGroup{g}
		return s, id
	}

	s, id := batching()
	plan := PlanFleet(p, s)
	if w := waitOf(t, plan, id).Wait; len(plan.Actions) > 0 || plan.BatchWait != s.BatchWait || w == nil || *w != WaitBatch ||
		marketPlan(t, plan, onDemand).Reason != WaitBatch {
		t.Fatalf("while arriving: actions %+v, batch %s, wait %+v, reason %q", plan.Actions, plan.BatchWait, waitOf(t, plan, id),
			marketPlan(t, plan, onDemand).Reason)
	}
	s.BatchWait = 0
	if plan := PlanFleet(p, s); len(actionsOf(plan, ActionBuy)) != 1 || plan.BatchWait != 0 {
		t.Fatalf("once settled: actions %+v, batch %s", plan.Actions, plan.BatchWait)
	}

	s, id = batching(planHost(1, planSmall, FleetStopped))
	if plan := PlanFleet(p, s); len(actionsOf(plan, ActionResume)) != 1 || len(plan.Actions) != 1 || plan.BatchWait != 0 ||
		*waitOf(t, plan, id).Host != (HostID{1}) {
		t.Fatalf("a fitting reserve: actions %+v, batch %s", plan.Actions, plan.BatchWait)
	}

	s, id = batching(planHost(1, planSmall, FleetStarting))
	if plan := PlanFleet(p, s); len(plan.Actions) > 0 || plan.BatchWait != 0 || *waitOf(t, plan, id).Host != (HostID{1}) {
		t.Fatalf("a starting host: actions %+v, batch %s, wait %+v", plan.Actions, plan.BatchWait, waitOf(t, plan, id))
	}
}

func TestGPUWorkResumesAReserveOfItsMarket(t *testing.T) {
	for _, c := range []struct {
		preemptible bool
		want        HostID
	}{{true, HostID{1}}, {false, HostID{2}}} {
		s := gpuPlanSnapshot(t, gpuReserve(t, 1, MarketSpot), gpuReserve(t, 2, MarketOnDemand))
		s.Pending = []DemandGroup{{Need: Requirement{Preemptible: c.preemptible, GPUs: []string{"L4"}, GPUCount: 1, CPUMillis: 1000, MemoryBytes: 4 * gib},
			Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow}}}}
		plan := PlanFleet(gpuPolicy(), s)
		if got := hostsOf(actionsOf(plan, ActionResume)); !slices.Equal(got, []HostID{c.want}) || len(boughtForWork(plan)) > 0 {
			t.Errorf("preemptible %t: resumed %v, bought %v", c.preemptible, got, boughtForWork(plan))
		}
	}
}

func TestAnyCardSpotWorkBorrowsWhileEveryReservedCardsSpotRefuses(t *testing.T) {
	s := gpuPlanSnapshot(t, gpuReserve(t, 2, MarketOnDemand))
	s.Offers.Spot = append(s.Offers.Spot, SpotQuote{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: "g6e.xlarge", HourlyMicros: 300_000, ObservedAt: offerNow})
	for _, model := range []string{"T4", "A10G", "L4"} {
		s.Offers.Cooldowns = append(s.Offers.Cooldowns, spotCooled(model)...)
	}
	g, _ := pendingOne(Requirement{Preemptible: true, GPUs: []string{GPUAny}, GPUCount: 1, CPUMillis: 1000, MemoryBytes: 4 * gib}, nil)
	s.Pending = []DemandGroup{g}
	plan := PlanFleet(gpuPolicy(), s)
	if resumed := hostsOf(actionsOf(plan, ActionResume)); !slices.Equal(resumed, []HostID{{2}}) {
		t.Fatalf("resumed %v, bought %v, want the on-demand L4 reserve", resumed, boughtForWork(plan))
	}
}

func TestSpotGPUWorkBuysOnDemandOnlyWhenNoModelsSpotCanHostIt(t *testing.T) {
	for _, c := range []struct {
		cooled []string
		want   string
	}{{[]string{"L4"}, "g5.2xlarge/spot"}, {[]string{"L4", "A10G"}, "g6.xlarge/on_demand"}} {
		s := gpuPlanSnapshot(t)
		s.Offers.OwnerPays = true
		for _, model := range c.cooled {
			s.Offers.Cooldowns = append(s.Offers.Cooldowns, spotCooled(model)...)
		}
		g, _ := pendingOne(Requirement{Preemptible: true, GPUs: []string{"L4", "A10G"}, GPUCount: 1, CPUMillis: 1000, MemoryBytes: 4 * gib}, nil)
		s.Pending = []DemandGroup{g}
		var got []string
		for _, a := range actionsOf(PlanFleet(gpuPolicy(), s), ActionBuy) {
			if len(a.Containers) > 0 {
				got = append(got, a.Offer.Type.Name+"/"+string(a.Offer.Market))
			}
		}
		if !slices.Equal(got, []string{c.want}) {
			t.Errorf("Spot of %v cooling: bought %v, want %s", c.cooled, got, c.want)
		}
	}
}

func TestPlanCoolsOnlyOffersTooSmallForTheirContainer(t *testing.T) {
	full := planHost(1, planLarge, FleetServing)
	full.Load, full.Containers = full.Usable, 1
	coming := planHost(2, planLarge, FleetStarting)
	tiny := planHost(3, planSmall, FleetServing)
	s := planSnapshot(t, full, coming, tiny)
	s.Pending = []DemandGroup{
		{Need: Requirement{CPUMillis: 4000, MemoryBytes: 8 * gib}, Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow, Host: &full.ID}}},
		{Need: Requirement{CPUMillis: 16000, MemoryBytes: 8 * gib}, Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow, Host: &tiny.ID}}},
	}
	_, cools := planOwner(planPolicy(FleetCapacity{}, FleetCapacity{}), s, time.Minute)
	if len(cools) != 1 || cools[0].InstanceType != planSmall.Name {
		t.Fatalf("cooled %+v, want only the offer of the host too small for its container", cools)
	}
}

func TestASizeCoolingForSecondsHoldsTheStepUp(t *testing.T) {
	in := offerInputs(t)
	for _, typ := range []string{"g6.xlarge", "g6.2xlarge", "g6.4xlarge"} {
		price, _ := mustType(t, typ).OnDemandMicros("us-east-2")
		in.Spot = append(in.Spot, SpotQuote{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: typ, HourlyMicros: price * 4 / 10, ObservedAt: offerNow})
	}
	in.Cooldowns = []OfferCooldown{{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: "g6.xlarge", Market: MarketSpot, Until: offerNow.Add(2 * time.Second)}}
	s := FleetSnapshot{Now: offerNow, Offers: in, HostRoom: 10, ReserveRoom: 10,
		Pending: []DemandGroup{{Need: Requirement{Preemptible: true, GPUs: []string{"L4"}, CPUMillis: 2000, MemoryBytes: 13056 << 20},
			Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow}}}}}
	if bought := boughtForWork(PlanFleet(connectionPolicy(DefaultPolicy()), s)); len(bought) > 0 {
		t.Fatalf("bought %v while g6.xlarge cools for two seconds", bought)
	}
	s.Now, s.Offers.Now = offerNow.Add(3*time.Second), offerNow.Add(3*time.Second)
	if bought := boughtForWork(PlanFleet(connectionPolicy(DefaultPolicy()), s)); !slices.Equal(bought, []string{"g6.xlarge"}) {
		t.Fatalf("bought %v once g6.xlarge freed, want a g6.xlarge", bought)
	}
}

// inMarket are the plan's actions in market m.
func inMarket(plan FleetPlan, m ReserveMarket) FleetPlan {
	plan.Actions = slices.DeleteFunc(slices.Clone(plan.Actions), func(a FleetAction) bool { return a.Market != m })
	return plan
}

// carried are the holds a pass leaves for the next one.
func carried(plan FleetPlan) map[ReserveMarket]MarketHolds {
	holds := map[ReserveMarket]MarketHolds{}
	for _, mp := range plan.Markets {
		holds[mp.Market] = mp.Holds
	}
	return holds
}

// gpuHost is an on-demand A10G host, running one job when busy.
func gpuHost(t *testing.T, id byte, typ string, busy bool) FleetHost {
	h := planHost(id, mustType(t, typ), FleetServing)
	h.GPU = "A10G"
	if busy {
		h.Load, h.Containers = FleetCapacity{CPUMillis: 1000, MemoryBytes: 20 * gib, GPUs: 1}, 1
	}
	return h
}

// A GPU market keeps no running headroom: a job holds its host, an idle
// host beside it leaves once its market's window has passed, and an idle
// host stays within the window for the next job, which takes it rather
// than a purchase.
func TestAGPUMarketKeepsOnlyTheHostsItsWorkHolds(t *testing.T) {
	a10g := ReserveMarket{GPU: "A10G"}
	s := gpuPlanSnapshot(t, gpuHost(t, 1, "g5.2xlarge", true), gpuHost(t, 2, "g5.2xlarge", false))
	plan := PlanFleet(gpuPolicy(), s)
	if left := hostsOf(actionsOf(plan, ActionDrain, ActionReturnToReserve)); !slices.Equal(left, []HostID{{2}}) || len(boughtTypes(plan)) > 0 {
		t.Fatalf("released %v, bought %v; want the idle A10G host to leave and nothing bought", left, boughtTypes(plan))
	}
	if mp := marketPlan(t, plan, a10g); !mp.WarmTarget.Empty() {
		t.Fatalf("A10G warm target %+v, want none", mp.WarmTarget)
	}

	s = gpuPlanSnapshot(t, gpuHost(t, 1, "g5.2xlarge", false))
	s.Holds = map[ReserveMarket]MarketHolds{a10g: {Running: Hold{Value: s.Hosts[0].Usable, At: offerNow.Add(-10 * time.Second)}}}
	g, id := pendingOne(Requirement{GPUs: []string{"A10G"}, GPUCount: 1, CPUMillis: 1000, MemoryBytes: 20 * gib}, nil)
	plan = PlanFleet(gpuPolicy(), s)
	if left := actionsOf(plan, ActionDrain, ActionReturnToReserve); len(left) > 0 {
		t.Fatalf("an A10G host released %v within its window", hostsOf(left))
	}
	s.Pending = []DemandGroup{g}
	if plan := PlanFleet(gpuPolicy(), s); len(actionsOf(inMarket(plan, a10g), ActionBuy, ActionResume)) > 0 || waitOf(t, plan, id).Wait != nil {
		t.Fatalf("the next job: actions %+v, wait %+v; want it placed on the idle host", plan.Actions, waitOf(t, plan, id))
	}
}

// An idle work host larger than the size cap drains rather than stop into
// the reserve its market lacks, which buys a reserve within the cap.
func TestAWorkHostAboveTheCapDrainsRatherThanJoinTheReserve(t *testing.T) {
	p := gpuPolicy()
	s := gpuPlanSnapshot(t, gpuHost(t, 1, "g5.8xlarge", false))
	s.Recent = map[ReserveMarket]FleetCapacity{{GPU: "A10G"}: {CPUMillis: 1000, MemoryBytes: 20 * gib, GPUs: 1}}
	plan := PlanFleet(p, s)
	if got := actionsOf(plan, ActionDrain, ActionReturnToReserve); len(got) != 1 || got[0].Kind != ActionDrain {
		t.Fatalf("actions %+v, want the g5.8xlarge drained", plan.Actions)
	}
	for _, a := range actionsOf(plan, ActionBuyReserve) {
		if !p.LargestShape.Cap.Covers(a.Offer.Usable) {
			t.Errorf("bought reserve %s above the size cap", a.Offer.Type.Name)
		}
	}
}

// An idle Spot GPU host on a persistent request stops into the reserve its
// market lacks instead of the market buying one.
func TestAnIdleGPUWorkHostBecomesItsMarketsMissingReserve(t *testing.T) {
	h := planHost(1, mustType(t, "g6.4xlarge"), FleetServing)
	h.Market, h.GPU = MarketSpot, "L4"
	s := gpuPlanSnapshot(t, h)
	plan := inMarket(PlanFleet(gpuPolicy(), s), ReserveMarket{Preemptible: true, GPU: "L4"})
	if got := actionsOf(plan, ActionReturnToReserve); len(got) != 1 || len(actionsOf(plan, ActionBuyReserve)) > 0 {
		t.Fatalf("actions %+v, want the L4 host returned and no reserve bought", plan.Actions)
	}
}

// No GPU reserve is bought above the size cap, even while every pool
// within it cools.
func TestAGPUReserveStaysWithinTheCapWhileSmallerPoolsCool(t *testing.T) {
	p := gpuPolicy()
	s := gpuPlanSnapshot(t)
	for _, typ := range []string{"g6.2xlarge", "g6.8xlarge", "g6.12xlarge", "g6.16xlarge"} {
		s.Offers.Spot = append(s.Offers.Spot, SpotQuote{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: typ, HourlyMicros: 900_000, ObservedAt: offerNow})
	}
	for _, typ := range []string{"g6.xlarge", "g6.2xlarge", "g6.4xlarge"} {
		s.Offers.Cooldowns = append(s.Offers.Cooldowns, OfferCooldown{Region: "us-east-2", InstanceType: typ, Market: MarketSpot, Until: offerNow.Add(time.Hour)})
	}
	for _, a := range actionsOf(PlanFleet(p, s), ActionBuyReserve) {
		if !p.LargestShape.Cap.Covers(a.Offer.Usable) || a.Offer.Type.GPUCount > 1 {
			t.Errorf("bought reserve %s/%s above the size cap", a.Offer.Type.Name, a.Offer.Market)
		}
	}
}

// An L4 job steps up one size while the smallest L4 type cools, and never
// to a type larger than the cap or with more cards while one within it
// is offered.
func TestAnL4JobStepsUpOneSizeWhileTheSmallestCools(t *testing.T) {
	s := gpuPlanSnapshot(t)
	s.Offers.OwnerPays = true
	for _, typ := range []string{"g6.2xlarge", "g6.8xlarge", "g6.12xlarge", "g6.16xlarge"} {
		s.Offers.Spot = append(s.Offers.Spot, SpotQuote{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: typ, HourlyMicros: 400_000, ObservedAt: offerNow})
	}
	s.Offers.Cooldowns = []OfferCooldown{{Region: "us-east-2", InstanceType: "g6.xlarge", Market: MarketSpot, Until: offerNow.Add(time.Hour)}}
	g, _ := pendingOne(l4Job, nil)
	s.Pending = []DemandGroup{g}
	if bought := boughtForWork(PlanFleet(gpuPolicy(), s)); !slices.Equal(bought, []string{"g6.2xlarge"}) {
		t.Fatalf("bought %v, want a g6.2xlarge", bought)
	}
}

// Spot-tolerant GPU work no Spot offer can host buys an on-demand host at a
// loss only once it has waited the Spot wait; until then it waits on the
// cooling Spot pools.
func TestSpotGPUWorkBuysOnDemandOnceItHasWaitedTheSpotWait(t *testing.T) {
	p := gpuPolicy()
	for _, c := range []struct {
		waited time.Duration
		buys   bool
	}{{0, false}, {p.SpotWait, true}} {
		s := gpuPlanSnapshot(t)
		s.Offers.Cooldowns = slices.Concat(spotCooled("L4"), spotCooled("A10G"))
		id := uuid.New()
		s.Pending = []DemandGroup{{Need: Requirement{Preemptible: true, GPUs: []string{"L4", "A10G"}, GPUCount: 1, CPUMillis: 1000, MemoryBytes: 4 * gib},
			Containers: []PendingContainer{{ID: id, Since: offerNow.Add(-c.waited)}}}}
		plan := PlanFleet(p, s)
		bought := boughtForWork(plan)
		if (len(bought) == 1) != c.buys {
			t.Errorf("after %s: bought %v, want a purchase %t", c.waited, bought, c.buys)
		}
		if w := waitOf(t, plan, id); !c.buys && (w.Wait == nil || *w.Wait != WaitCooldown) {
			t.Errorf("after %s: wait %+v, want the cooling Spot pools", c.waited, w)
		}
	}
}

// A stopping Spot reserve whose request EC2 has held active past
// SpotDisableMax no longer holds work back: the work buys.
func TestWorkStopsWaitingForAStoppingReserveStuckPastTheDisableBound(t *testing.T) {
	p := planPolicy(FleetCapacity{}, FleetCapacity{})
	for _, c := range []struct {
		stopped time.Duration
		resumes bool
	}{{p.Starts.SpotDisable, true}, {p.Starts.SpotDisableMax + time.Second, false}} {
		h := planHost(1, planSmall, FleetStopping)
		h.Market, h.StoppedAt = MarketSpot, ptr(offerNow.Add(-c.stopped))
		s := planSnapshot(t, h)
		g, _ := pendingOne(Requirement{Preemptible: true, CPUMillis: 1000, MemoryBytes: gib}, nil)
		s.Pending = []DemandGroup{g}
		plan := PlanFleet(p, s)
		if resumed := len(actionsOf(plan, ActionResume)) == 1; resumed != c.resumes || !c.resumes && len(boughtForWork(plan)) != 1 {
			t.Errorf("stopped %s ago: actions %+v", c.stopped, plan.Actions)
		}
	}
}

// A market that holds its running target buys no headroom; one that falls
// short beyond its floor buys it.
func TestHeadroomIsBoughtOnlyWhatTheKeptHostsLack(t *testing.T) {
	p := planPolicy(cpuGiB(1000, 2), FleetCapacity{})
	h := planHost(1, planSmall, FleetServing)
	h.Load, h.Containers = cpuGiB(4000, 8), 4
	if plan := PlanFleet(p, planSnapshot(t, h)); len(boughtTypes(plan)) > 0 {
		t.Fatalf("bought %v while 4 CPU of load leaves 4 CPU free on the host", boughtTypes(plan))
	}
	h.Load = cpuGiB(7500, 15)
	if plan := PlanFleet(p, planSnapshot(t, h)); len(boughtTypes(plan)) != 1 {
		t.Fatalf("bought %v with 7.5 CPU of load on 8 CPU, want headroom", boughtTypes(plan))
	}
}

// Work that takes the market's floor buys a host with it free at once,
// sized to the floor rather than the market's load.
func TestWorkThatTakesTheFloorBuysAHostWithItFree(t *testing.T) {
	p := planPolicy(cpuGiB(1000, 2), FleetCapacity{})
	busy := planHost(1, planLarge, FleetServing)
	busy.Load, busy.Containers = large, 4
	if bought := boughtTypes(PlanFleet(p, planSnapshot(t, busy))); !slices.Equal(bought, []string{planSmall.Name}) {
		t.Fatalf("bought %v once work took the floor, want one %s", bought, planSmall.Name)
	}
	busy.Load = large.Minus(cpuGiB(1000, 2))
	if bought := boughtTypes(PlanFleet(p, planSnapshot(t, busy))); len(bought) > 0 {
		t.Fatalf("bought %v while the busy host keeps the floor free", bought)
	}
}

// A market that buys keeps every host it holds for its window, then lets
// what its target no longer holds leave.
func TestAMarketKeepsItsHostsForTheWindowAfterItGrows(t *testing.T) {
	p := planPolicy(small, FleetCapacity{})
	busy := planHost(1, planSmall, FleetServing)
	busy.Load, busy.Containers = small, 1
	plan := PlanFleet(p, planSnapshot(t, busy))
	if bought := boughtTypes(plan); len(bought) != 1 {
		t.Fatalf("bought %v once work took the floor, want one host", bought)
	}
	s := planSnapshot(t, planHost(1, planSmall, FleetServing), planHost(2, planSmall, FleetServing))
	s.Holds = carried(plan)
	s.Now = s.Now.Add(p.Window(onDemand) - holdStep)
	if left := actionsOf(PlanFleet(p, s), ActionDrain, ActionReturnToReserve); len(left) > 0 {
		t.Fatalf("released %v within the window after the market grew", hostsOf(left))
	}
	s.Now = s.Now.Add(holdStep)
	if left := actionsOf(PlanFleet(p, s), ActionDrain, ActionReturnToReserve); len(left) != 1 {
		t.Fatalf("released %v once the window passed, want one host", hostsOf(left))
	}
}

// A launch that walks refused pools raises no hold: past the window of the
// purchase that made it, the market keeps only the idle hosts its target
// holds, and the launch nothing waits for is cancelled.
func TestALaunchStuckInRefusedPoolsKeepsNoIdleHost(t *testing.T) {
	p := planPolicy(small, FleetCapacity{})
	stuck := planHost(1, planSmall, FleetStarting)
	stuck.Requested = true
	plan := PlanFleet(p, planSnapshot(t, stuck, planHost(2, planSmall, FleetServing), planHost(3, planSmall, FleetServing)))
	if left := actionsOf(plan, ActionDrain, ActionReturnToReserve); len(left) != 1 || len(actionsOf(plan, ActionCancel)) != 1 {
		t.Fatalf("actions %+v, want one idle host to leave and the launch cancelled", plan.Actions)
	}
}

// A market's stopped target holds for the cost horizon after its load
// falls, so the reserves a burst needed wait for the next one.
func TestReservesForALoadPeakStayForTheCostHorizon(t *testing.T) {
	p := planPolicy(FleetCapacity{}, small)
	s := planSnapshot(t, planHost(1, planSmall, FleetStopped), planHost(2, planSmall, FleetStopped))
	s.Holds = map[ReserveMarket]MarketHolds{onDemand: {Stopped: Hold{Value: small.Times(2), At: offerNow.Add(-10 * time.Minute)}}}
	if retired := actionsOf(PlanFleet(p, s), ActionRetireReserve); len(retired) > 0 {
		t.Fatalf("retired %v ten minutes after the peak", hostsOf(retired))
	}
	s.Holds[onDemand] = MarketHolds{Stopped: Hold{Value: small.Times(2), At: offerNow.Add(-p.CostHorizon)}}
	if retired := actionsOf(PlanFleet(p, s), ActionRetireReserve); len(retired) != 1 {
		t.Fatalf("retired %v once the horizon passed, want one reserve", hostsOf(retired))
	}
}

// l4Job is a Spot-tolerant job of one L4 card.
var l4Job = Requirement{Preemptible: true, GPUs: []string{"L4"}, GPUCount: 1, CPUMillis: 2000, MemoryBytes: 13056 << 20}

// A reserve resumed for work serves it and holds none of the stopped
// target any more: the next pass buys its replacement.
func TestAReserveResumedForWorkIsReplacedInTheReserve(t *testing.T) {
	p := planPolicy(FleetCapacity{}, small)
	s := planSnapshot(t, planHost(1, planSmall, FleetStopped))
	g, _ := pendingOne(Requirement{CPUMillis: 4000, MemoryBytes: 4 * gib}, nil)
	s.Pending = []DemandGroup{g}
	plan := PlanFleet(p, s)
	if resumed := hostsOf(actionsOf(plan, ActionResume)); !slices.Equal(resumed, []HostID{{1}}) {
		t.Fatalf("actions %+v, want the reserve resumed for the work", plan.Actions)
	}
	serving := planHost(1, planSmall, FleetServing)
	serving.Load, serving.Containers, serving.Slept = cpuGiB(4000, 4), 1, true
	s = planSnapshot(t, serving)
	s.Holds = carried(plan)
	plan = PlanFleet(p, s)
	var bought FleetCapacity
	for _, a := range actionsOf(plan, ActionBuyReserve) {
		bought = bought.Plus(a.Offer.Usable)
	}
	if !bought.Covers(small) {
		t.Fatalf("actions %+v, want a reserve bought to replace the one serving work", plan.Actions)
	}
}

// A reserve shortfall is bought in one cover: the largest shape and the
// rest of the stopped target share the cheapest hosts, here one host of
// twice the size rather than one for the shape and another for the rest.
func TestAReserveShortfallIsBoughtInOneCover(t *testing.T) {
	double := CatalogType{Name: "m.double", Topology: twoPerCore(32), MemoryBytes: 32 * gib, prices: [4]int64{150_000, 150_000, 150_000, 150_000}}
	p := planPolicy(FleetCapacity{}, small)
	p.OnDemand.FitLargest, p.LargestShape.Default = true, FleetCapacity{}
	s := planSnapshot(t)
	s.Offers.Catalog = append(s.Offers.Catalog, double)
	s.Recent = map[ReserveMarket]FleetCapacity{onDemand: small}
	reserves := actionsOf(PlanFleet(p, s), ActionBuyReserve)
	if len(reserves) != 1 || reserves[0].Offer.Type.Name != double.Name {
		t.Fatalf("bought reserves %+v, want one %s for the shape and the rest", reserves, double.Name)
	}
}

// Headroom resumes the reserve that holds all the market lacks before
// cheaper ones that hold part of it, so one resume does.
func TestHeadroomResumesTheReserveThatHoldsAllItLacks(t *testing.T) {
	p := planPolicy(cpuGiB(1000, 2), FleetCapacity{})
	s := planSnapshot(t, planHost(1, planSmall, FleetStopped), planHost(2, planSmall, FleetStopped), planHost(3, planLarge, FleetStopped))
	s.Arrived = map[ReserveMarket]FleetCapacity{onDemand: cpuGiB(20_000, 20)}
	plan := PlanFleet(p, s)
	if got := hostsOf(actionsOf(plan, ActionResume)); !slices.Equal(got, []HostID{{3}}) || len(actionsOf(plan, ActionBuy)) > 0 {
		t.Fatalf("actions %+v, want the large reserve resumed alone", plan.Actions)
	}
}

// full is a serving m.small its work fills.
func full(id byte) FleetHost {
	h := planHost(id, planSmall, FleetServing)
	h.Load, h.Containers = small, 1
	return h
}

// As load ramps up, the work each step brings buys at once, and the floor
// the ramp took grows back once its shortfall has lasted the window; nothing
// leaves during the ramp. Once the load falls, the hosts the ramp bought
// stay for the window after the last purchase, then what the target no
// longer holds leaves.
func TestARampBuysItsWorkAtOnceAndItsHeadroomOnceTheShortfallLasts(t *testing.T) {
	p := planPolicy(cpuGiB(1000, 2), FleetCapacity{})
	w := p.Window(onDemand)
	step := func(s FleetSnapshot, at time.Duration, work, headroom, left int) FleetPlan {
		t.Helper()
		s.Now = offerNow.Add(at)
		plan := PlanFleet(p, s)
		bought, forWork, l := boughtTypes(plan), boughtForWork(plan), actionsOf(plan, ActionDrain, ActionReturnToReserve)
		if len(forWork) != work || len(bought)-len(forWork) != headroom || len(l) != left ||
			slices.ContainsFunc(bought, func(n string) bool { return n != planSmall.Name }) {
			t.Fatalf("at %s bought %v, %v for work, and released %v; want %d for work, %d for headroom and %d released",
				at, bought, forWork, hostsOf(l), work, headroom, left)
		}
		return plan
	}
	plan := step(planSnapshot(t, full(1)), 0, 0, 1, 0)
	s := planSnapshot(t, full(1), full(2))
	g, _ := pendingOne(Requirement{CPUMillis: small.CPUMillis, MemoryBytes: small.MemoryBytes}, nil)
	s.Pending, s.Holds = []DemandGroup{g}, carried(plan)
	plan = step(s, 20*time.Second, 1, 0, 0)
	s = planSnapshot(t, full(1), full(2), full(3))
	s.Holds = carried(plan)
	plan = step(s, 20*time.Second+w, 0, 1, 0)
	s = planSnapshot(t, full(1), planHost(2, planSmall, FleetServing), planHost(3, planSmall, FleetServing), planHost(4, planSmall, FleetServing))
	s.Holds = carried(plan)
	plan = step(s, 30*time.Second+w, 0, 0, 0)
	s.Holds = carried(plan)
	step(s, 20*time.Second+2*w, 0, 0, 2)
}

// A job every three minutes that fills the market's floor host for 40
// seconds starts on that host each time and buys nothing: the floor it
// takes frees within the window, so the running target never rises.
func TestARecurringJobOnTheFloorBuysNothing(t *testing.T) {
	p := planPolicy(small, FleetCapacity{})
	job := Requirement{CPUMillis: small.CPUMillis, MemoryBytes: small.MemoryBytes}
	settled := planSnapshot(t, planHost(1, planSmall, FleetServing))
	settled.Now = offerNow.Add(-holdStep)
	holds := carried(PlanFleet(p, settled))
	for at := time.Duration(0); at < 9*time.Minute; at += holdStep {
		into := at % (3 * time.Minute)
		host := planHost(1, planSmall, FleetServing)
		if into > 0 && into <= 40*time.Second {
			host.Load, host.Containers = small, 1
		}
		s := planSnapshot(t, host)
		s.Now, s.Holds = offerNow.Add(at), holds
		var id uuid.UUID
		if into == 0 {
			var g DemandGroup
			g, id = pendingOne(job, nil)
			s.Pending = []DemandGroup{g}
		}
		plan := PlanFleet(p, s)
		if into == 0 && waitOf(t, plan, id).Wait != nil || len(plan.Actions) > 0 {
			t.Fatalf("at %s: waits %+v, actions %+v; want the job on the floor host and nothing bought", at, plan.Waits, plan.Actions)
		}
		holds = carried(plan)
	}
}

// A headroom shortfall smaller than the market's smallest host buys
// nothing on its own; one beyond a host buys.
func TestASubHostHeadroomShortfallBuysNothing(t *testing.T) {
	p := planPolicy(cpuGiB(1000, 2), FleetCapacity{})
	busy := planHost(1, planSmall, FleetServing)
	busy.Load, busy.Containers = cpuGiB(6500, 8), 1
	s := planSnapshot(t, busy)
	s.Arrived = map[ReserveMarket]FleetCapacity{onDemand: cpuGiB(1000, 1)}
	if plan := PlanFleet(p, s); len(plan.Actions) > 0 {
		t.Fatalf("actions %+v for a half-CPU shortfall, want none", plan.Actions)
	}
	s.Arrived[onDemand] = cpuGiB(9000, 16)
	if bought := boughtTypes(PlanFleet(p, s)); len(bought) == 0 {
		t.Fatalf("bought nothing for a shortfall beyond a host")
	}
}
