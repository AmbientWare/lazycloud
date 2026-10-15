package compute

import (
	"cmp"
	"fmt"
	"maps"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

// The replay (fleet_run_test.go) runs PlanFleet against a fleet simulation
// calibrated to the recorded prod run (testdata/fleetrun): measured
// lifecycle timings, placement oldest first and rate-card revenue. A pass
// runs as the plan loop's do: when the fleet or its work changed, after a
// pass that acted, and otherwise on the loop's tick. Each purchase launches
// in its pool on the launch loop's tick, walking on through the pools a
// replay's EC2 refuses, and a stopped Spot reserve's request disables after
// a measured gap.

const (
	simTick = time.Second
	// From prod's host lifecycle logs of 2026-10-09, beside the start times
	// the policy plans with: a hibernated reserve resumes to its first
	// container in 9-13 s, and a bought reserve stops about 170 s after the
	// purchase. simStop, how long a serving host takes to stop into the
	// reserve, is assumed.
	simResume  = 12 * time.Second
	simPrepare = 147 * time.Second
	simStop    = time.Minute
	// A GPU host's stop takes 392 s, the median of prod's GPU hosts and
	// the 2026-10-13 EC2 trace (305-410 s), which EC2 bills as stopped. The
	// guest powers off within 15 s; EC2 holds a GPU instance in stopping
	// for the rest, on any image and when forced.
	simGPUStop = 392 * time.Second
	// simCooldown is how long a refusal cools its pool, the fleet's
	// CapacityCooldown.
	simCooldown = 10 * time.Minute
)

// fleetTick is the period of the fleet's plan and launch loops, fleetTick
// in cmd/scheduler.
const fleetTick = 5 * time.Second

// disableGaps are measured gaps between a Spot reserve's stop and EC2
// disabling its persistent request: 38-146 s, with 63 s and 165 s on
// 2026-10-13.
var disableGaps = []time.Duration{38 * time.Second, 45 * time.Second, 52 * time.Second, 58 * time.Second, 63 * time.Second,
	66 * time.Second, 70 * time.Second, 80 * time.Second, 90 * time.Second, 146 * time.Second, 165 * time.Second}

type simHost struct {
	FleetHost
	offer FleetOffer

	until      time.Time
	containers []*simContainer
	// failAt, when set, is when the host fails without serving, as one whose
	// bootstrap timed out.
	failAt *time.Time
	// unbilledFrom, on a GPU reserve being made, is when EC2 begins
	// stopping it, which it bills as stopped.
	unbilledFrom time.Time
	// walk is set while the host is requested.
	walk *simWalk
	// startAt, on a resumed reserve, is when EC2 starts its instance; its
	// stop's record holds until then.
	startAt time.Time
	// served is set once a container ran on the host.
	served bool
}

// simWalk is a requested host's launch: what the launcher reads of it and
// the containers waiting for it.
type simWalk struct {
	row     ClaimHostActionsRow
	waiters []HostWaitersRow
	kind    FleetActionKind
}

// stopAt has h, made a reserve, begin its stop at from: a GPU host's stop
// takes simGPUStop, which EC2 bills as stopped.
func (h *simHost) stopAt(from time.Time) {
	h.until = from
	if h.GPU != "" {
		h.unbilledFrom, h.until = from, from.Add(simGPUStop)
	}
}

type simContainer struct {
	id      uuid.UUID
	need    Requirement
	arrived time.Time
	runs    time.Duration
	ends    time.Time
	host    *HostID
	// market is the reserve market its placement counts in: its GPU
	// model's, else its CPU market.
	market ReserveMarket
	// fn names the work.
	fn string
}

type simArrival struct {
	at    time.Duration
	need  Requirement
	count int
	runs  time.Duration
	fn    string
}

type simResult struct {
	launches, refusals int
	violations         []string
	// actions counts the plans' actions by kind and reason.
	actions map[string]int
	// unusedSpotCPU counts the Spot CPU hosts that ended without running a
	// container.
	unusedSpotCPU int
	// fnWaits are each function's start waits.
	fnWaits map[string][]time.Duration
	// windowMicros is spend within the measure window, in µ$·s.
	windowMicros int64
}

// simCool is a pool's run of refusals.
type simCool struct {
	lasted time.Duration
	until  time.Time
	// walk is the host whose launch walk refused the pool first.
	walk *simHost
}

type sim struct {
	t       *testing.T
	p       Policy
	in      OfferInputs
	now     time.Time
	start   time.Time
	hosts   []*simHost
	pending []*simContainer
	r       simResult
	// recent is the largest recent shape per market the planner reads, and
	// holds carries each market's held targets from one pass to the next,
	// as the published plan does.
	recent map[ReserveMarket]FleetCapacity
	holds  map[ReserveMarket]MarketHolds
	// maxHosts is the fleet limit; provision is how long a launch takes to
	// serve.
	maxHosts int
	// provision is how long a launch takes to serve, and gpuProvision an
	// on-demand GPU one's when set, else the policy's GPU launch.
	provision, gpuProvision time.Duration
	// refuses names the pools EC2 refuses for capacity, and cools are the
	// runs of refusals by pool.
	refuses func(region, zoneID, instanceType string, market Market) bool
	cools   map[OfferCooldown]*simCool
	// prices are the Spot quotes in force at a time.
	prices func(time.Time) []SpotQuote
	// until ends the window spend and capacity are measured over.
	until time.Time
	// placedLog is every container placed.
	placedLog []*simContainer
	next      int
	// launchedAt is when the launcher last ran a pass, and wake is set
	// when a plan pass bought, as the compute notification wakes it.
	launchedAt time.Time
	wake       bool
	// planned is the state the last pass left, plannedAt when it ran and
	// acted whether it acted.
	planned   string
	plannedAt time.Time
	acted     bool
}

// state is the fleet and its work as far as a change wakes the plan loop:
// pending containers, and each host's phase, containers and launch.
func (s *sim) state() string {
	var b strings.Builder
	fmt.Fprint(&b, len(s.pending))
	for _, h := range s.hosts {
		fmt.Fprint(&b, h.ID, h.State, len(h.containers), h.walk != nil, ";")
	}
	return b.String()
}

// gap is how long EC2 takes to disable h's request once it stopped.
func (s *sim) gap(h *simHost) time.Duration {
	return disableGaps[(int(h.ID[0])<<8|int(h.ID[1]))%len(disableGaps)]
}

func (s *sim) run(d time.Duration, arrivals []simArrival) simResult {
	for s.now.Before(s.start.Add(d)) {
		s.advance()
		for _, a := range arrivals {
			if at := s.start.Add(a.at); !at.Before(s.now) && at.Before(s.now.Add(simTick)) {
				for range a.count {
					s.pending = append(s.pending, &simContainer{id: uuid.New(), need: a.need, arrived: s.now, runs: a.runs, fn: a.fn})
				}
			}
		}
		s.place()
		if s.wake || s.now.Sub(s.launchedAt) >= fleetTick {
			s.launchPass()
		}
		if state := s.state(); state != s.planned || s.acted || s.now.Sub(s.plannedAt) >= fleetTick {
			// A pass that held purchases for arrivals runs again once they
			// settle, as the plan loop does.
			if wait := s.plan(); wait > 0 {
				tick := s.now
				s.now = s.now.Add(wait)
				s.plan()
				if s.wake {
					s.launchPass()
				}
				s.now = tick
			}
			s.planned, s.plannedAt = s.state(), s.now
		}
		if s.wake {
			s.launchPass()
		}
		s.account()
		s.now = s.now.Add(simTick)
	}
	return s.r
}

// advance moves hosts through their timed phases and ends containers.
func (s *sim) advance() {
	s.hosts = slices.DeleteFunc(s.hosts, func(h *simHost) bool { return h.failAt != nil && !s.now.Before(*h.failAt) })
	for _, h := range s.hosts {
		if h.walk != nil || h.until.After(s.now) {
			continue
		}
		switch {
		case h.State == FleetStarting:
			h.State, h.PhaseAt = FleetServing, s.now
		case h.State == FleetPreparing && h.Market == MarketSpot:
			// Its instance stopped; EC2 disables the Spot request later.
			h.State, h.StoppedAt, h.until = FleetStopping, ptr(s.now), s.now.Add(s.gap(h))
			if h.unbilledFrom.IsZero() {
				h.unbilledFrom = s.now
			}
		case (h.State == FleetPreparing || h.State == FleetStopping) && *h.ReserveMode == ReserveHibernate:
			h.State = FleetImageSaved
		case h.State == FleetPreparing || h.State == FleetStopping:
			h.State = FleetStopped
		}
	}
	for _, h := range s.hosts {
		h.containers = slices.DeleteFunc(h.containers, func(c *simContainer) bool {
			return !c.ends.After(s.now)
		})
	}
	// A draining host that runs nothing terminates.
	s.hosts = slices.DeleteFunc(s.hosts, func(h *simHost) bool {
		gone := h.State == FleetDraining && len(h.containers) == 0
		if gone && !h.served && h.market().spotCPU() {
			s.r.unusedSpotCPU++
		}
		return gone
	})
}

// place puts pending containers on serving hosts as placement does: those
// whose bought host serves first, on it while it has room, then the oldest
// first, as PendingContainers orders them, each on the host ChooseHost
// picks. Containers that arrived in one tick keep their order.
func (s *sim) place() {
	servingHost := func(c *simContainer) int {
		if c.host == nil {
			return -1
		}
		return slices.IndexFunc(s.hosts, func(h *simHost) bool { return h.ID == *c.host && h.State == FleetServing })
	}
	slices.SortStableFunc(s.pending, func(a, b *simContainer) int {
		return cmp.Or(boolOrder(servingHost(a) < 0, servingHost(b) < 0), a.arrived.Compare(b.arrived))
	})
	s.pending = slices.DeleteFunc(s.pending, func(c *simContainer) bool {
		var serving []*simHost
		var rooms []HostCapacity
		for _, h := range s.hosts {
			if h.State == FleetServing {
				fh := s.fleetHost(h)
				serving, rooms = append(serving, h), append(rooms, forWork(fh, fh.capacity()))
			}
		}
		i := -1
		if c.host != nil {
			i = slices.IndexFunc(serving, func(h *simHost) bool { return h.ID == *c.host })
			if i >= 0 && !rooms[i].Fits(c.need) {
				i = -1
			}
		}
		if i < 0 {
			i = ChooseHost(rooms, c.need, s.p.OnDemand.Warm.Floor)
		}
		if i < 0 {
			return false
		}
		best := serving[i]
		switch {
		case !c.need.Preemptible && best.Market == MarketSpot:
			s.r.violations = append(s.r.violations, fmt.Sprintf("work that cannot run on Spot placed on Spot %s", best.InstanceType))
		case c.need.Region != "" && ProductRegion(best.Region) != c.need.Region:
			s.r.violations = append(s.r.violations, fmt.Sprintf("work pinned to %s placed in %s", c.need.Region, best.Region))
		case c.need.GPUsNeeded() > 0 && !GPUAccepted(c.need.GPUs, best.GPU):
			s.r.violations = append(s.r.violations, fmt.Sprintf("work for %v placed on %q", c.need.GPUs, best.GPU))
		}
		c.ends = s.now.Add(c.runs)
		c.market = ReserveMarket{Preemptible: c.need.Preemptible}
		if c.need.GPUsNeeded() > 0 {
			c.market = ReserveMarket{GPU: best.offer.Type.GPU}
		}
		best.containers, best.served = append(best.containers, c), true
		s.placedLog = append(s.placedLog, c)
		if s.r.fnWaits == nil {
			s.r.fnWaits = map[string][]time.Duration{}
		}
		s.r.fnWaits[c.fn] = append(s.r.fnWaits[c.fn], s.now.Sub(c.arrived))
		return true
	})
}

// unwait returns the containers waiting for host id to waiting for none.
func (s *sim) unwait(id HostID) {
	for _, c := range s.pending {
		if c.host != nil && *c.host == id {
			c.host = nil
		}
	}
}

func (s *sim) fleetHost(h *simHost) FleetHost {
	fh := h.FleetHost
	fh.Requested = h.walk != nil
	if fh.State != FleetStopping && !s.now.Before(h.startAt) {
		fh.StoppedAt = nil
	}
	fh.Load, fh.Containers, fh.UsedDisks = FleetCapacity{}, 0, 0
	for _, c := range h.containers {
		fh.Load, fh.UsedDisks = fh.Load.Plus(reservedShape(c.need)), fh.UsedDisks+c.need.Disks
		fh.Containers++
	}
	return fh
}

// batchWait is how long the arrival batch stays open, as the BatchWait
// query reads it: among the containers of the lookback still pending or
// placed on a host bought for them.
func (s *sim) batchWait() time.Duration {
	w := s.p.Batch
	var arrivals []time.Time
	for _, c := range s.pending {
		arrivals = append(arrivals, c.arrived)
	}
	for _, h := range s.hosts {
		for _, c := range h.containers {
			if c.host != nil {
				arrivals = append(arrivals, c.arrived)
			}
		}
	}
	slices.SortFunc(arrivals, time.Time.Compare)
	prev := s.now.Add(-(w.Max + w.Quiet))
	var newest, began time.Time
	for _, at := range arrivals {
		if at.Before(prev) {
			continue
		}
		if at.Sub(prev) >= w.Quiet {
			began = at
		}
		prev, newest = at, at
	}
	if began.IsZero() {
		return 0
	}
	return max(min(newest.Add(w.Quiet).Sub(s.now), began.Add(w.Max).Sub(s.now)), 0)
}

// plan runs one pass and returns how long it held purchases.
func (s *sim) plan() time.Duration {
	// Arrived is what the Spot CPU market's placed work created within the
	// arrival window reserves, less its largest batch, as RecentShapes reads
	// it: a run is arrivals less than the batch's quiet apart, cut into
	// batches of its max from the run's first; older containers drop out of
	// the log.
	s.placedLog = slices.DeleteFunc(s.placedLog, func(c *simContainer) bool { return s.now.Sub(c.arrived) >= s.p.WarmWindow })
	slices.SortStableFunc(s.placedLog, func(a, b *simContainer) int { return a.arrived.Compare(b.arrived) })
	started, burst := map[ReserveMarket]FleetCapacity{}, map[ReserveMarket]FleetCapacity{}
	byMarket := map[ReserveMarket][]*simContainer{}
	var spot []*simContainer
	for _, c := range s.placedLog {
		started[c.market] = started[c.market].Upper(reservedShape(c.need))
		byMarket[c.market] = append(byMarket[c.market], c)
		if s.now.Sub(c.arrived) >= s.p.ArrivalWindow {
			continue
		}
		if c.need.Preemptible && c.need.GPUsNeeded() == 0 {
			spot = append(spot, c)
		}
	}
	// Burst is each market's largest batch within the warm window, batches
	// formed as RecentShapes forms them.
	for m, cs := range byMarket {
		var batch, largest FleetCapacity
		var run, last time.Time
		part := -1
		for _, c := range cs {
			if c.arrived.Sub(last) >= s.p.Batch.Quiet {
				run, part = c.arrived, -1
			}
			if n := int(c.arrived.Sub(run) / s.p.Batch.Max); n != part {
				largest, batch, part = largest.Upper(batch), FleetCapacity{}, n
			}
			batch, last = batch.Plus(reservedShape(c.need)), c.arrived
		}
		burst[m] = largest.Upper(batch)
	}
	slices.SortStableFunc(spot, func(a, b *simContainer) int { return a.arrived.Compare(b.arrived) })
	var sum, largest, batch FleetCapacity
	var run, last time.Time
	part := -1
	for _, c := range spot {
		if c.arrived.Sub(last) >= s.p.Batch.Quiet {
			run, part = c.arrived, -1
		}
		if n := int(c.arrived.Sub(run) / s.p.Batch.Max); n != part {
			largest, batch, part = largest.Upper(batch), FleetCapacity{}, n
		}
		shape := reservedShape(c.need)
		sum, batch, last = sum.Plus(shape), batch.Plus(shape), c.arrived
	}
	arrived := map[ReserveMarket]FleetCapacity{{Preemptible: true}: sum.Minus(largest.Upper(batch)).Clamp()}
	s.in.Spot = s.prices(s.now)
	snapshot := FleetSnapshot{Now: s.now, Recent: s.recent, Started: started, Burst: burst, Arrived: arrived, Offers: s.offers(), BatchWait: s.batchWait()}
	live, reserves := 0, 0
	for _, h := range s.hosts {
		snapshot.Hosts = append(snapshot.Hosts, s.fleetHost(h))
		if h.reserve() {
			reserves++
		} else {
			live++
		}
	}
	snapshot.HostRoom, snapshot.ReserveRoom = s.maxHosts-live, s.maxHosts-reserves
	groups := map[string]*DemandGroup{}
	var order []string
	for _, c := range s.pending {
		key := fmt.Sprint(c.need)
		if groups[key] == nil {
			groups[key] = &DemandGroup{Need: c.need}
			order = append(order, key)
		}
		groups[key].Containers = append(groups[key].Containers, PendingContainer{ID: c.id, Since: c.arrived, Host: c.host})
	}
	for _, key := range order {
		snapshot.Pending = append(snapshot.Pending, *groups[key])
	}
	snapshot.Holds = s.holds
	plan := PlanFleet(s.p, snapshot)
	s.holds = map[ReserveMarket]MarketHolds{}
	for _, mp := range plan.Markets {
		s.holds[mp.Market] = mp.Holds
	}
	s.check(snapshot, plan)
	s.apply(plan)
	s.acted = len(plan.Actions) > 0
	return plan.BatchWait
}

// check asserts every action names its reason, no purchase serves a
// container a ready reserve would fit, and, since the simulation never
// refuses on-demand capacity, that each on-demand purchase takes its
// type's cheapest pool.
func (s *sim) check(snapshot FleetSnapshot, plan FleetPlan) {
	used := maps.Clone(QuotaUse(snapshot.Hosts, s.in.Catalog))
	for _, a := range plan.Actions {
		if a.Reason == "" {
			s.r.violations = append(s.r.violations, fmt.Sprintf("%s: %s action names no reason", s.now.Format(time.TimeOnly), a.Kind))
		}
		if a.Offer == nil {
			continue
		}
		in := snapshot.Offers
		in.Cooldowns, in.QuotaUsed = nil, maps.Clone(used)
		used[a.Offer.Quota] += a.Offer.Type.VCPUs()
		if a.Offer.Market != MarketOnDemand {
			continue
		}
		reserve := a.Kind == ActionBuyReserve
		cost := servingCost(s.p)
		need := Requirement{CPUMillis: a.Offer.Usable.CPUMillis, MemoryBytes: a.Offer.Usable.MemoryBytes}
		for _, o := range RankOffers(s.p, need, reserve, in) {
			if o.Type.Name == a.Offer.Type.Name && o.Market == MarketOnDemand && cost(o) < cost(*a.Offer) {
				s.r.violations = append(s.r.violations, fmt.Sprintf("%s bought %s while %s costs less", s.now.Format(time.TimeOnly), a.Offer.Key(), o.Key()))
				break
			}
		}
	}
	resumed := map[HostID]bool{}
	for _, a := range plan.Actions {
		if a.Kind == ActionResume {
			resumed[*a.Host] = true
		}
	}
	for _, a := range plan.Actions {
		if a.Kind != ActionBuy || len(a.Containers) == 0 {
			continue
		}
		for _, h := range snapshot.Hosts {
			if !h.resumable() || resumed[h.ID] {
				continue
			}
			for _, c := range s.pending {
				if slices.Contains(a.Containers, c.id) && !c.need.Preemptible && h.capacity().Fits(c.need) {
					s.r.violations = append(s.r.violations, fmt.Sprintf("%s bought %s while reserve %s fits", s.now.Format(time.TimeOnly), a.Offer.Type.Name, h.InstanceType))
				}
			}
		}
	}
}

// offers are the offer inputs at now. The scheduler refreshes Spot quotes
// far more often than they age out.
func (s *sim) offers() OfferInputs {
	in := s.in
	in.Now, in.Spot = s.now, slices.Clone(s.in.Spot)
	for i := range in.Spot {
		in.Spot[i].ObservedAt = s.now
	}
	in.Cooldowns = slices.Clone(s.in.Cooldowns)
	for key, c := range s.cools {
		if c.until.After(s.now) {
			key.Until = c.until
			in.Cooldowns = append(in.Cooldowns, key)
		}
	}
	return in
}

func (s *sim) host(id HostID) *simHost {
	i := slices.IndexFunc(s.hosts, func(h *simHost) bool { return h.ID == id })
	if i < 0 {
		s.t.Fatalf("no host %s", id)
	}
	return s.hosts[i]
}

// purchase inserts the host a purchase buys, requested in the pool the
// planner picked, for the launcher to launch.
func (s *sim) purchase(a FleetAction, waiters []HostWaitersRow) HostID {
	o := *a.Offer
	reserve := a.Kind == ActionBuyReserve
	row := ClaimHostActionsRow{
		Market: ptr(string(o.Market)), GpuType: o.Type.GPU, GpuCount: int32(o.Type.GPUCount),
		CpuMillis: o.Usable.CPUMillis, MemoryBytes: o.Usable.MemoryBytes,
		HoldsCpuMillis: ptr(a.Holds.CPUMillis), HoldsMemoryBytes: ptr(a.Holds.MemoryBytes),
		InstanceType: o.Type.Name, Region: o.Region, AvailabilityZone: o.Zone, MarginWaived: a.Reason == ReasonSpotWait,
	}
	if a.Kind == ActionRightsize {
		row.Replaces = ptr(uuid.UUID(*a.Host))
	}
	if reserve {
		row.ReserveMode, row.HoldsDisks = ptr(string(*a.Mode)), ptr(int16(o.Type.DiskSlots()))
	}
	s.next++
	h := &simHost{offer: o, FleetHost: FleetHost{
		ID: HostID{byte(s.next >> 8), byte(s.next), 0xf1}, InstanceType: o.Type.Name, Region: o.Region, Zone: o.Zone, ZoneID: o.ZoneID, Market: o.Market,
		GPU: o.Type.GPU, Usable: o.Usable, State: FleetStarting, HourlyMicros: ptr(o.HourlyMicros),
		HibernationConfigured: reserve && o.Hibernate, Stoppable: reserve || o.Market == MarketOnDemand || o.Type.GPU != "",
	}}
	if a.Kind == ActionRightsize {
		h.Replaces = a.Host
	}
	if reserve {
		h.State, h.ReserveMode, h.Slept = FleetPreparing, a.Mode, true
	}
	h.walk = &simWalk{row: row, waiters: waiters, kind: a.Kind}
	s.hosts = append(s.hosts, h)
	s.wake = true
	return h.ID
}

// launchPass is one launcher pass: each requested host tries its pool,
// then the next pools fallbackPool picks while EC2 refuses, up to
// passPools pools this pass.
func (s *sim) launchPass() {
	s.launchedAt, s.wake = s.now, false
	for _, h := range slices.Clone(s.hosts) {
		if h.walk == nil {
			continue
		}
		for tried := 1; ; tried++ {
			done, moved := s.tryPool(h, tried == 1)
			if done || !moved || tried == passPools {
				break
			}
		}
	}
}

// tryPool launches h in its pool, as launchHost does. EC2 refuses it when
// refuses names its pool, which cools the pool's zone; a refused launch
// moves to the pool fallbackPool picks, and without one the host fails. A
// GPU walk claimed in a pool the size rule no longer keeps moves on
// uncooled, as launchPools does. It reports whether the walk ended and
// whether it moved.
func (s *sim) tryPool(h *simHost, first bool) (bool, bool) {
	w, o := h.walk, h.offer
	all := func(string, bool) bool { return true }
	stored := !first || o.Type.GPU == "" || w.row.Replaces != nil
	if !stored {
		_, keeps := walkPools(s.p, w.row, w.waiters, s.offers(), all)
		stored = keeps(FleetOffer{Type: o.Type, Market: o.Market, Usable: o.Usable})
	}
	if stored {
		if s.refuses == nil || !s.refuses(o.Region, o.ZoneID, o.Type.Name, o.Market) {
			s.launched(h)
			return true, false
		}
		s.r.refusals++
		s.walkCool(h, OfferCooldown{Region: o.Region, ZoneID: o.ZoneID, InstanceType: o.Type.Name, Market: o.Market})
	}
	next, ok := FleetOffer{}, false
	if w.row.Replaces == nil {
		next, ok = fallbackPool(s.p, w.row, w.waiters, s.offers(), all)
	}
	if !ok {
		if w.row.Replaces != nil {
			s.host(HostID(*w.row.Replaces)).RightsizeRefusedAt = ptr(s.now)
		}
		s.hosts = slices.DeleteFunc(s.hosts, func(o *simHost) bool { return o == h })
		s.unwait(h.ID)
		return true, false
	}
	h.offer, h.InstanceType, h.Region, h.Zone, h.ZoneID, h.Usable, h.GPU = next, next.Type.Name, next.Region, next.Zone, next.ZoneID, next.Usable, next.Type.GPU
	h.HourlyMicros = ptr(next.HourlyMicros)
	w.row.Region, w.row.AvailabilityZone, w.row.InstanceType, w.row.LaunchPools = next.Region, next.Zone, next.Type.Name, w.row.LaunchPools+1
	return false, true
}

// walkCool cools a pool h's walk refused as coolPool does: InsertCooldown
// records the walk, then AlignWalk cools every pool of the walk until the
// latest of their ends.
func (s *sim) walkCool(h *simHost, c OfferCooldown) {
	s.cool(c, h)
	var end time.Time
	for _, c := range s.cools {
		if c.walk == h && c.until.After(end) {
			end = c.until
		}
	}
	for _, c := range s.cools {
		if c.walk == h {
			c.until = end
		}
	}
}

// cool cools c's pool as InsertCooldown does, refused by by: for
// simCooldown, keeping its end and the host that refused it first while it
// cools, and after its cooldown ended twice as long as that one lasted, up
// to cooldownGrowth cooldowns.
func (s *sim) cool(c OfferCooldown, by *simHost) {
	if s.cools == nil {
		s.cools = map[OfferCooldown]*simCool{}
	}
	key := OfferCooldown{Region: c.Region, ZoneID: c.ZoneID, InstanceType: c.InstanceType, Market: c.Market}
	last, ok := s.cools[key]
	switch {
	case !ok:
		s.cools[key] = &simCool{lasted: simCooldown, until: s.now.Add(simCooldown), walk: by}
	case last.until.After(s.now):
		if last.walk == nil {
			last.walk = by
		}
	default:
		last.lasted = min(max(2*last.lasted, simCooldown), cooldownGrowth*simCooldown)
		last.until, last.walk = s.now.Add(last.lasted), by
	}
}

// launched starts h's instance in its pool.
func (s *sim) launched(h *simHost) {
	w, o := h.walk, h.offer
	h.walk = nil
	if w.kind == ActionRightsize {
		if replaced := s.host(*h.Replaces); o.running() >= replaced.offer.running() {
			s.r.violations = append(s.r.violations, fmt.Sprintf("%s replaced %s with %s, which costs no less",
				s.now.Format(time.TimeOnly), replaced.offer.Key(), o.Key()))
		}
	}
	provision := s.provision
	switch {
	case o.Type.GPU != "" && o.Market == MarketOnDemand && s.gpuProvision > 0:
		provision = s.gpuProvision
	case o.Type.GPU != "":
		provision = s.p.Starts.GPULaunch
	}
	for _, zone := range []string{o.ZoneID, ""} {
		delete(s.cools, OfferCooldown{Region: o.Region, ZoneID: zone, InstanceType: o.Type.Name, Market: o.Market})
	}
	h.until = s.now.Add(provision)
	if h.State == FleetPreparing {
		h.stopAt(s.now.Add(provision + simPrepare))
	}
	s.r.launches++
}

func (s *sim) apply(plan FleetPlan) {
	bought := map[int]HostID{}
	if s.r.actions == nil {
		s.r.actions = map[string]int{}
	}
	for i, a := range plan.Actions {
		s.r.actions[string(a.Kind)+"/"+string(a.Reason)]++
		switch a.Kind {
		case ActionBuy, ActionRightsize, ActionBuyReserve:
			var waiters []HostWaitersRow
			for _, w := range plan.Waits {
				if w.Action != nil && *w.Action == i {
					c := s.pending[slices.IndexFunc(s.pending, func(c *simContainer) bool { return c.id == w.Container })]
					waiters = append(waiters, HostWaitersRow{Preemptible: c.need.Preemptible, Disks: int32(c.need.Disks), Region: c.need.Region, Zone: c.need.Zone})
				}
			}
			bought[i] = s.purchase(a, waiters)
		case ActionResume:
			h := s.host(*a.Host)
			// EC2 starts a stopping Spot reserve once it disables the
			// request, and the actuator records the start a tick after it
			// does; until then a stopping one keeps its stop's record, as
			// ResumeReserves does.
			from := s.now
			switch {
			case h.State != FleetStopping:
				h.StoppedAt = nil
			case h.until.After(from):
				from = h.until
			}
			h.startAt = from.Add(fleetTick)
			switch {
			case h.State == FleetImageSaved || h.State == FleetStopping && *h.ReserveMode == ReserveHibernate:
				h.until = from.Add(simResume)
			case h.GPU != "":
				h.until = from.Add(s.p.Starts.GPUResume)
			default:
				h.until = from.Add(s.p.Starts.Resume)
			}
			h.State, h.ReserveMode, h.unbilledFrom = FleetStarting, nil, time.Time{}
		case ActionReturnToReserve:
			h := s.host(*a.Host)
			h.State, h.ReserveMode, h.Slept = FleetPreparing, a.Mode, true
			h.stopAt(s.now.Add(simStop))
			h.HandsBack = false
			h.offer.StoppedMicros = rootDiskMicros(h.Region, rootVolumeGiB, baselineMiBps)
		case ActionDrain:
			s.host(*a.Host).State, s.host(*a.Host).HandsBack = FleetDraining, false
		case ActionHandBack, ActionLend:
			s.host(*a.Host).HandsBack = a.Kind == ActionHandBack
		case ActionCancel:
			s.hosts = slices.DeleteFunc(s.hosts, func(h *simHost) bool { return h.ID == *a.Host })
			s.unwait(*a.Host)
		case ActionRetireReserve:
			s.hosts = slices.DeleteFunc(s.hosts, func(h *simHost) bool { return h.ID == *a.Host })
		}
	}
	for _, w := range plan.Waits {
		i := slices.IndexFunc(s.pending, func(c *simContainer) bool { return c.id == w.Container })
		switch {
		case w.Host != nil:
			s.pending[i].host = w.Host
		case w.Action != nil:
			if id, ok := bought[*w.Action]; ok {
				s.pending[i].host = ptr(id)
			}
		}
	}
}

// account adds a tick of every host's cost while within the measure
// window.
func (s *sim) account() {
	if !s.now.Before(s.until) {
		return
	}
	secs := int64(simTick / time.Second)
	for _, h := range s.hosts {
		if h.resumable() || !h.unbilledFrom.IsZero() && !s.now.Before(h.unbilledFrom) {
			s.r.windowMicros += h.offer.StoppedMicros * secs
			continue
		}
		s.r.windowMicros += h.offer.HourlyMicros * secs
	}
}

// prodNetworks are the platform's four regions and their zones.
func prodNetworks() map[string]Network {
	zones := func(region string, ids ...string) Network {
		var n Network
		for i, id := range ids {
			n.Subnets = append(n.Subnets, Subnet{ID: "subnet-" + id, Zone: region + string(rune('a'+i)), ZoneID: id})
		}
		return n
	}
	return map[string]Network{
		"us-east-1": zones("us-east-1", "use1-az6", "use1-az1", "use1-az2", "use1-az4", "use1-az3", "use1-az5"),
		"us-east-2": zones("us-east-2", "use2-az1", "use2-az2", "use2-az3"),
		"us-west-1": zones("us-west-1", "usw1-az3", "usw1-az1"),
		"us-west-2": zones("us-west-2", "usw2-az2", "usw2-az1", "usw2-az3", "usw2-az4"),
	}
}
