package compute

import (
	"fmt"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/billing"
)

// The replays (fleet_run_test.go) run PlanFleet against a fleet
// simulation calibrated to the recorded prod runs (testdata/fleetrun):
// measured lifecycle timings, placement oldest first and rate-card revenue.
// Launches walk refused pools through fallbackPool a pass budget at a
// time, on the launch loop's tick, with cooldowns as the launcher's queries
// keep them, and a stopped Spot reserve's request disables after a
// measured gap.

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
	// default CapacityCooldown.
	simCooldown = 10 * time.Minute
)

type simHost struct {
	FleetHost
	offer FleetOffer

	until      time.Time
	containers []*simContainer
	// served is set once a container ran on the host, and headroom on one
	// bought for no pending work.
	served, headroom bool
	// failAt, when set, is when the host fails without serving, as one whose
	// bootstrap timed out.
	failAt *time.Time
	// resumed is set on a host resumed from the reserve to serve, and
	// servedAt is when it last began to serve.
	resumed  bool
	servedAt time.Time
	// unbilledFrom, on a GPU reserve being made, is when EC2 begins
	// stopping it, which it bills as stopped.
	unbilledFrom time.Time
	// walk is set while the host is requested: its launch through refused
	// pools.
	walk *simWalk
	// startAt, on a resumed reserve, is when EC2 starts its instance; its
	// stop's record holds until then.
	startAt time.Time
	// startRefusedAt is when EC2 last refused to start the reserve.
	startRefusedAt time.Time
}

// simWalk is a requested host's launch: what the launcher reads of it,
// the containers waiting for it, its purchase record and the end its
// refused pools share.
type simWalk struct {
	row      ClaimHostActionsRow
	waiters  []HostWaitersRow
	purchase *simPurchase
	kind     FleetActionKind
	// refused are the pools the walk refused.
	refused []OfferCooldown
}

// stopAt has h, made a reserve, begin its stop at from: a GPU host's stop
// takes simGPUStop, which EC2 bills as stopped.
func (h *simHost) stopAt(from time.Time) {
	h.until = from
	if h.GPU != "" {
		h.unbilledFrom, h.until = from, from.Add(simGPUStop)
	}
}

// billed reports whether EC2 bills h as running.
func (s *sim) billed(h *simHost) bool {
	return !h.resumable() && (h.unbilledFrom.IsZero() || s.now.Before(h.unbilledFrom))
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
	// fn names the work; started is when it was placed.
	fn      string
	started time.Time
	// retry is set on work an eviction returned to pending, and packed on
	// CPU work placed on a GPU host.
	retry, packed bool
}

type simArrival struct {
	at    time.Duration
	need  Requirement
	count int
	runs  time.Duration
	fn    string
}

type simResult struct {
	// spendMicros is what the fleet cost, in µ$·s, and windowMicros and
	// fleetCPUSec the spend and running hosts' CPU seconds within the
	// measure window.
	spendMicros, windowMicros int64
	fleetCPUSec               float64
	// revenueNanos is what finished containers bill.
	revenueNanos int64
	// waits are the start waits, retries apart, and fnWaits each
	// function's.
	waits   []time.Duration
	fnWaits map[string][]time.Duration
	// launches counts launched hosts and refusals the launches EC2 refused.
	launches, refusals int
	// bought are the purchases to serve, with every pool each launch
	// tried.
	bought []simPurchase
	// unusedBy counts by market the serving hosts that left without running
	// a container, unusedHeadroom those of them bought for headroom, and
	// mostServing the most hosts of each market that served at once.
	unusedBy, mostServing map[ReserveMarket]int
	unusedHeadroom        int
	// violations are placements no host may take, and breaches the
	// reserves the passes left above the size cap.
	violations, breaches []string
}

// simPurchase is one reserve purchase: its market and each pool its launch
// tried, and whether it launched.
type simPurchase struct {
	at       time.Time
	market   ReserveMarket
	tried    []FleetOffer
	launched bool
	// holds is what the purchase was bought to hold, and containers the
	// work it was bought for.
	holds      FleetCapacity
	containers int
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
	// recentDisks the most disks one attached.
	recent      map[ReserveMarket]FleetCapacity
	recentDisks map[ReserveMarket]int
	// holds are what the last published plan carries to the next pass.
	holds map[ReserveMarket]MarketHolds
	// maxHosts is the fleet limit; tick is the step, simTick unless a long
	// scenario sets it; provision is how long a CPU launch takes to serve,
	// and gpuProvision an on-demand GPU one's when set, else the policy's
	// GPU launch.
	maxHosts                int
	tick                    time.Duration
	provision, gpuProvision time.Duration
	// refuses names the pools EC2 refuses for capacity. A refused launch
	// cools its zone and moves to the pool fallbackPool picks, as the
	// launcher does.
	refuses func(region, zoneID, instanceType string, market Market) bool
	// prices, when set, are the Spot quotes in force at a time.
	prices func(time.Time) []SpotQuote
	// until ends the window spend and capacity are measured over; zero is
	// the whole run.
	until time.Time
	// placedLog is every container placed within the arrival window.
	placedLog []*simContainer
	// cools are the runs of refusals by pool.
	cools map[OfferCooldown]*simCool
	next  int
	// launchedAt is when the launcher last ran a pass, and wake is set
	// when a plan pass bought, as the compute notification wakes it.
	launchedAt time.Time
	wake       bool
	// plannedAt is when the last plan pass ran, and planWake is set when a
	// container arrived, was placed or ended since, as their notifications
	// wake the planner on every replica.
	plannedAt time.Time
	planWake  bool
}

// planTick is the planner loop's period on the leader, fleetTick in
// cmd/scheduler.
const planTick = 5 * time.Second

// planRepeats bounds the passes one wake runs: a pass that acts notifies
// the compute channel, which wakes the planner loops again at once.
const planRepeats = 3

// launchTick is the launch loop's period, fleetTick in cmd/scheduler.
const launchTick = 5 * time.Second

// disableGaps are measured gaps between a Spot reserve's stop and EC2
// disabling its persistent request: 38-146 s, with 63 s and 165 s on
// 2026-10-13.
var disableGaps = []time.Duration{38 * time.Second, 45 * time.Second, 52 * time.Second, 58 * time.Second, 63 * time.Second,
	66 * time.Second, 70 * time.Second, 80 * time.Second, 90 * time.Second, 146 * time.Second, 165 * time.Second}

// gap is how long EC2 takes to disable h's request once it stopped.
func (s *sim) gap(h *simHost) time.Duration {
	return disableGaps[(int(h.ID[0])<<8|int(h.ID[1]))%len(disableGaps)]
}

func (s *sim) run(d time.Duration, arrivals []simArrival) simResult {
	if s.until.IsZero() {
		s.until = s.start.Add(d)
	}
	for s.now.Before(s.start.Add(d)) {
		s.advance()
		for _, a := range arrivals {
			if at := s.start.Add(a.at); !at.Before(s.now) && at.Before(s.now.Add(s.tick)) {
				for range a.count {
					s.pending = append(s.pending, &simContainer{id: uuid.New(), need: a.need, arrived: s.now, runs: a.runs, fn: a.fn})
				}
				s.planWake = true
			}
		}
		s.place()
		if s.wake || s.now.Sub(s.launchedAt) >= launchTick {
			s.launchPass()
		}
		if s.planWake || s.now.Sub(s.plannedAt) >= planTick {
			s.planWake = false
			for range planRepeats {
				wait, acted := s.plan()
				// A pass that held purchases for arrivals runs again once they
				// settle, as the planner loop does.
				if wait > 0 {
					tick := s.now
					s.now = s.now.Add(wait)
					s.plan()
					if s.wake {
						s.launchPass()
					}
					s.now = tick
				}
				if s.wake {
					s.launchPass()
				}
				if !acted {
					break
				}
			}
		}
		s.account()
		s.now = s.now.Add(s.tick)
	}
	for _, h := range s.hosts {
		for _, c := range h.containers {
			s.bill(c, s.now.Sub(c.started))
		}
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
			h.State, h.servedAt = FleetServing, s.now
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
			if c.ends.After(s.now) {
				return false
			}
			s.bill(c, c.runs)
			s.planWake = true
			return true
		})
	}
	// A draining host that runs nothing terminates.
	s.hosts = slices.DeleteFunc(s.hosts, func(h *simHost) bool {
		gone := h.State == FleetDraining && len(h.containers) == 0
		if gone && !h.served {
			if s.r.unusedBy == nil {
				s.r.unusedBy = map[ReserveMarket]int{}
			}
			s.r.unusedBy[h.market()]++
			if h.headroom {
				s.r.unusedHeadroom++
			}
		}
		return gone
	})
	serving := map[ReserveMarket]int{}
	for _, h := range s.hosts {
		if h.State == FleetServing {
			serving[h.market()]++
		}
	}
	if s.r.mostServing == nil {
		s.r.mostServing = map[ReserveMarket]int{}
	}
	for m, n := range serving {
		s.r.mostServing[m] = max(s.r.mostServing[m], n)
	}
}

// place puts pending containers on serving hosts as placement does: the
// oldest first, as PendingContainers orders them, each on the host
// ChooseHost picks. Containers that arrived in one tick keep their order.
func (s *sim) place() {
	slices.SortStableFunc(s.pending, func(a, b *simContainer) int { return a.arrived.Compare(b.arrived) })
	var retries []*simContainer
	defer func() { s.pending = append(s.pending, retries...) }()
	s.pending = slices.DeleteFunc(s.pending, func(c *simContainer) bool {
		var serving []*simHost
		var rooms []HostCapacity
		for _, h := range s.hosts {
			if h.State == FleetServing {
				serving, rooms = append(serving, h), append(rooms, s.load(h))
			}
		}
		i := ChooseHost(rooms, c.need, s.p.OnDemand.Floor)
		if i < 0 {
			return false
		}
		best := serving[i]
		if rooms[i].Evicts(c.need) {
			retries = append(retries, s.evict(best, func(room HostCapacity) bool { return !room.Evicts(c.need) })...)
		}
		switch {
		case !c.need.Preemptible && best.Market == MarketSpot:
			s.r.violations = append(s.r.violations, fmt.Sprintf("work that cannot run on Spot placed on Spot %s", best.InstanceType))
		case c.need.Region != "" && ProductRegion(best.Region) != c.need.Region:
			s.r.violations = append(s.r.violations, fmt.Sprintf("work pinned to %s placed in %s", c.need.Region, best.Region))
		case c.need.GPUsNeeded() > 0 && !GPUAccepted(c.need.GPUs, best.GPU):
			s.r.violations = append(s.r.violations, fmt.Sprintf("work for %v placed on %q", c.need.GPUs, best.GPU))
		}
		c.ends, c.started, c.packed = s.now.Add(c.runs), s.now, best.GPU != "" && c.need.GPUsNeeded() == 0
		c.market = ReserveMarket{Preemptible: c.need.Preemptible}
		if c.need.GPUsNeeded() > 0 {
			c.market = ReserveMarket{GPU: best.offer.Type.GPU}
		}
		// RecentShapes reads placed containers by tolerance and GPU model.
		shaped := ReserveMarket{Preemptible: c.need.Preemptible, GPU: c.market.GPU}
		if s.recent == nil {
			s.recent, s.recentDisks = map[ReserveMarket]FleetCapacity{}, map[ReserveMarket]int{}
		}
		if s.recentDisks == nil {
			s.recentDisks = map[ReserveMarket]int{}
		}
		s.recent[shaped] = s.recent[shaped].Upper(reservedShape(c.need))
		s.recentDisks[shaped] = max(s.recentDisks[shaped], c.need.Disks)
		best.containers, best.served = append(best.containers, c), true
		s.placedLog = append(s.placedLog, c)
		s.planWake = true
		if c.retry {
			return true
		}
		wait := s.now.Sub(c.arrived)
		if wait > 20*time.Second { fmt.Printf("DBG wait %s %s arrived %s placed on %s %s\n", c.fn, wait, c.arrived.Sub(s.start), best.InstanceType, best.ID) }
		s.r.waits = append(s.r.waits, wait)
		if s.r.fnWaits == nil {
			s.r.fnWaits = map[string][]time.Duration{}
		}
		s.r.fnWaits[c.fn] = append(s.r.fnWaits[c.fn], wait)
		return true
	})
}

// evict stops the CPU work on GPU host h, the latest placed first, until
// enough reports its room suffices, and returns it as retries to start
// again. The platform absorbs what the evicted runs would bill.
func (s *sim) evict(h *simHost, enough func(HostCapacity) bool) []*simContainer {
	var retries []*simContainer
	packed := slices.DeleteFunc(slices.Clone(h.containers), func(c *simContainer) bool { return !c.packed })
	slices.SortStableFunc(packed, func(a, b *simContainer) int { return b.started.Compare(a.started) })
	room := s.load(h)
	for _, c := range packed {
		if enough(room) {
			break
		}
		room.Evict(reservedShape(c.need))
		h.containers = slices.DeleteFunc(h.containers, func(o *simContainer) bool { return o == c })
		retries = append(retries, &simContainer{id: uuid.New(), need: c.need, arrived: s.now, runs: c.runs, fn: c.fn, retry: true})
	}
	return retries
}

func (s *sim) load(h *simHost) HostCapacity {
	fh := s.fleetHost(h)
	return fh.capacity()
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
	fh.Current, fh.Requested = true, h.walk != nil
	if fh.State != FleetStopping && !s.now.Before(h.startAt) {
		fh.StoppedAt = nil
	}
	fh.StartRefused = !h.startRefusedAt.IsZero() && s.now.Sub(h.startRefusedAt) < simCooldown
	fh.Load, fh.Lent, fh.Packed, fh.Containers, fh.UsedDisks = FleetCapacity{}, FleetCapacity{}, FleetCapacity{}, 0, 0
	var tolerant FleetCapacity
	for _, c := range h.containers {
		fh.Load, fh.UsedDisks = fh.Load.Plus(reservedShape(c.need)), fh.UsedDisks+c.need.Disks
		if c.packed {
			fh.Packed = fh.Packed.Plus(reservedShape(c.need))
			continue
		}
		fh.Containers++
		if c.need.Preemptible {
			tolerant = tolerant.Plus(reservedShape(c.need))
		}
	}
	if h.Market == MarketOnDemand {
		fh.Lent = tolerant
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

// plan runs one pass and returns how long it held purchases and whether
// it acted.
func (s *sim) plan() (time.Duration, bool) {
	s.plannedAt = s.now
	// Arrived is what each CPU market's work placed within the arrival
	// window reserves, less its largest batch, as RecentShapes reads it: a
	// run is arrivals less than the batch's quiet apart, cut into batches of
	// its max from the run's first; containers placed earlier drop out of
	// the log.
	window := s.p.ArrivalWindow
	s.placedLog = slices.DeleteFunc(s.placedLog, func(c *simContainer) bool { return s.now.Sub(c.started) >= window })
	arrived := map[ReserveMarket]FleetCapacity{}
	for _, m := range []ReserveMarket{{Preemptible: true}, {}} {
		var work []*simContainer
		for _, c := range s.placedLog {
			if c.need.Preemptible == m.Preemptible && c.need.GPUsNeeded() == 0 {
				work = append(work, c)
			}
		}
		slices.SortStableFunc(work, func(a, b *simContainer) int { return a.arrived.Compare(b.arrived) })
		var sum, largest, batch FleetCapacity
		var run, last time.Time
		part := -1
		for _, c := range work {
			if c.arrived.Sub(last) >= s.p.Batch.Quiet {
				run, part = c.arrived, -1
			}
			if n := int(c.arrived.Sub(run) / s.p.Batch.Max); n != part {
				largest, batch, part = largest.Upper(batch), FleetCapacity{}, n
			}
			shape := reservedShape(c.need)
			sum, batch, last = sum.Plus(shape), batch.Plus(shape), c.arrived
		}
		arrived[m] = sum.Minus(largest.Upper(batch)).Clamp()
	}
	if s.prices != nil {
		s.in.Spot = s.prices(s.now)
	}
	snapshot := FleetSnapshot{Now: s.now, Recent: s.recent, RecentDisks: s.recentDisks, Arrived: arrived, Offers: s.offers(), BatchWait: s.batchWait()}
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
	s.watch(plan)
	s.apply(plan)
	return plan.BatchWait, len(plan.Actions) > 0
}

// watch records each reserve, bought or returned, above the size cap.
func (s *sim) watch(plan FleetPlan) {
	limit := s.p.LargestShape.Cap
	for _, a := range plan.Actions {
		switch {
		case a.Kind == ActionBuyReserve && !limit.Covers(a.Offer.Usable):
			s.r.breaches = append(s.r.breaches, fmt.Sprintf("%s bought reserve %s above the size cap", s.now.Format(time.TimeOnly), a.Offer.Type.Name))
		case a.Kind == ActionReturnToReserve && !limit.Covers(s.host(*a.Host).Usable):
			s.r.breaches = append(s.r.breaches, fmt.Sprintf("%s returned %s above the size cap", s.now.Format(time.TimeOnly), s.host(*a.Host).InstanceType))
		}
	}
}

// offers are the offer inputs at now, with the pools cooling now. The
// scheduler refreshes Spot quotes far more often than they age out.
func (s *sim) offers() OfferInputs {
	in := s.in
	in.Now, in.Spot = s.now, slices.Clone(s.in.Spot)
	for i := range in.Spot {
		in.Spot[i].ObservedAt = s.now
	}
	in.Cooldowns = slices.Clone(s.in.Cooldowns)
	for key, c := range s.cools {
		if c.until.After(s.now) {
			key.Until, key.Quota = c.until, c.quota
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
		InstanceType: o.Type.Name, Region: o.Region, AvailabilityZone: o.Zone,
	}
	if a.Kind == ActionRightsize {
		row.Replaces = ptr(uuid.UUID(*a.Host))
	}
	if reserve {
		row.ReserveMode, row.HoldsDisks = ptr(string(*a.Mode)), ptr(int16(o.Type.DiskSlots()))
	}
	s.next++
	h := &simHost{offer: o, FleetHost: FleetHost{Replaces: a.Host,
		ID: HostID{byte(s.next >> 8), byte(s.next), 0xf1}, InstanceType: o.Type.Name, Region: o.Region, Zone: o.Zone, ZoneID: o.ZoneID, Market: o.Market,
		GPU: o.Type.GPU, Usable: o.Usable, State: FleetStarting, HourlyMicros: ptr(o.HourlyMicros),
		HibernationConfigured: reserve && o.Hibernate, Stoppable: reserve || o.Market == MarketOnDemand || o.Type.GPU != "",
	}}
	if a.Kind != ActionRightsize {
		h.Replaces = nil
	}
	if reserve {
		h.State, h.ReserveMode, h.Slept = FleetPreparing, a.Mode, true
	}
	h.headroom = a.Kind == ActionBuy && len(a.Containers) == 0
	h.walk = &simWalk{row: row, waiters: waiters, kind: a.Kind,
		purchase: &simPurchase{at: s.now, market: a.Market, holds: a.Holds, containers: len(a.Containers)}}
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
// it would exceed its vCPU quota, cooling with quotas known the whole
// class, or when refuses names its pool, cooling the pool's zone. A
// refused launch moves to the pool fallbackPool picks; without one the
// host fails. It reports whether the walk ended and whether it moved.
func (s *sim) tryPool(h *simHost, first bool) (bool, bool) {
	w, o := h.walk, h.offer
	all := func(string, bool) bool { return true }
	// A GPU walk claimed in a pool the size rule no longer keeps moves on
	// uncooled, as launchPools does.
	stored := !first || o.Type.GPU == "" || w.row.Replaces != nil
	if !stored {
		_, keeps := walkPools(s.p, w.row, w.waiters, s.offers(), all)
		stored = keeps(FleetOffer{Type: o.Type, Market: o.Market, Usable: o.Usable})
	}
	if stored {
		w.purchase.tried = append(w.purchase.tried, o)
		cool := OfferCooldown{Region: o.Region, InstanceType: o.Type.Name, Market: o.Market}
		if s.refuses != nil && s.refuses(o.Region, o.ZoneID, o.Type.Name, o.Market) {
			cool.ZoneID = o.ZoneID
		} else {
			s.launched(h)
			return true, false
		}
		s.r.refusals++
		s.walkCool(h, cool)
	}
	next, ok := FleetOffer{}, false
	if w.row.Replaces == nil {
		next, ok = fallbackPool(s.p, w.row, w.waiters, s.offers(), all)
	}
	if !ok {
		if w.row.Replaces != nil {
			s.host(HostID(*w.row.Replaces)).RightsizeRefusedAt = ptr(s.now)
		}
		s.record(w)
		s.hosts = slices.DeleteFunc(s.hosts, func(o *simHost) bool { return o == h })
		s.unwait(h.ID)
		ends := map[time.Time]bool{}
		for _, k := range w.refused {
			if c := s.cools[k]; c.walk == h {
				ends[c.until] = true
			}
		}
		if len(ends) > 1 {
			s.r.violations = append(s.r.violations, fmt.Sprintf("%s a walk's %d refused pools cool until %d ends",
				s.now.Format(time.TimeOnly), len(w.refused), len(ends)))
		}
		return true, false
	}
	h.offer, h.InstanceType, h.Region, h.Zone, h.ZoneID, h.Usable, h.GPU = next, next.Type.Name, next.Region, next.Zone, next.ZoneID, next.Usable, next.Type.GPU
	h.HourlyMicros = ptr(next.HourlyMicros)
	w.row.Region, w.row.AvailabilityZone, w.row.InstanceType, w.row.LaunchPools = next.Region, next.Zone, next.Type.Name, w.row.LaunchPools+1
	return false, true
}

// launched starts h's instance in its pool.
func (s *sim) launched(h *simHost) {
	w, o := h.walk, h.offer
	h.walk = nil
	w.purchase.launched = true
	s.record(w)
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
	h.until = s.now.Add(provision)
	if h.State == FleetPreparing {
		h.stopAt(s.now.Add(provision + simPrepare))
	}
	for _, zone := range []string{o.ZoneID, ""} {
		delete(s.cools, OfferCooldown{Region: o.Region, ZoneID: zone, InstanceType: o.Type.Name, Market: o.Market})
	}
	s.r.launches++
}

// record keeps a finished walk's purchase.
func (s *sim) record(w *simWalk) {
	switch w.kind {
	case ActionBuy:
		s.r.bought = append(s.r.bought, *w.purchase)
	case ActionBuyReserve, ActionResume, ActionReturnToReserve, ActionDrain, ActionRetireReserve, ActionCancel, ActionRightsize, ActionHandBack, ActionLend:
	}
}

// walkCool cools a pool h's walk refused as coolPool does: InsertCooldown
// records the walk, then AlignWalk cools every pool of the walk until the
// latest of their ends.
func (s *sim) walkCool(h *simHost, c OfferCooldown) {
	key := s.cool(c, h)
	h.walk.refused = append(h.walk.refused, key)
	var end time.Time
	for _, c := range s.cools {
		if c.walk == h {
			end = maxTime(end, c.until)
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
// to cooldownGrowth cooldowns. It returns the pool's key.
func (s *sim) cool(c OfferCooldown, by *simHost) OfferCooldown {
	if s.cools == nil {
		s.cools = map[OfferCooldown]*simCool{}
	}
	key := OfferCooldown{Region: c.Region, ZoneID: c.ZoneID, InstanceType: c.InstanceType, Market: c.Market}
	last, ok := s.cools[key]
	switch {
	case !ok:
		s.cools[key] = &simCool{lasted: simCooldown, until: s.now.Add(simCooldown), quota: c.Quota, walk: by}
	case last.until.After(s.now):
		last.quota = c.Quota
		if last.walk == nil {
			last.walk = by
		}
	default:
		last.lasted = min(max(2*last.lasted, simCooldown), cooldownGrowth*simCooldown)
		last.until, last.quota, last.walk = s.now.Add(last.lasted), c.Quota, by
	}
	return key
}

// simCool is a pool's cooldown: how long it lasted and when it ends, and
// whether it cools the pool's quota class.
type simCool struct {
	lasted time.Duration
	until  time.Time
	quota  bool
	// walk is the host whose launch walk refused the pool first.
	walk *simHost
}

func maxTime(a, b time.Time) time.Time {
	if a.After(b) {
		return a
	}
	return b
}

func (s *sim) apply(plan FleetPlan) {
	bought := map[int]HostID{}
	for _, a := range plan.Actions {
		name := ""
		if a.Host != nil { if h := s.host(*a.Host); h != nil { name = h.InstanceType + " " + string(h.State) } }
		if a.Offer != nil { name = a.Offer.Type.Name + " holds " + describe(a.Holds) + fmt.Sprintf(" for %d", len(a.Containers)) }
		fmt.Printf("DBG %s %s %s %s\n", s.now.Sub(s.start), a.Market, a.Kind, name)
	}
	for _, w := range plan.Waits { if w.Wait != nil && *w.Wait != WaitProvisioning { fmt.Printf("DBG waitwhy %s %s\n", s.now.Sub(s.start), *w.Wait) } }
	for i, a := range plan.Actions {
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
			if s.refuses != nil && s.refuses(h.Region, h.ZoneID, h.InstanceType, h.Market) {
				// EC2 refusing the start leaves the reserve stopped with the
				// refusal recorded and cools its zone, as the actuator does.
				s.r.refusals++
				s.cool(OfferCooldown{Region: h.Region, ZoneID: h.ZoneID, InstanceType: h.InstanceType, Market: h.Market}, nil)
				h.startRefusedAt = s.now
				continue
			}
			// EC2 starts a stopping Spot reserve once it disables the
			// request, and the actuator records the start a tick after it
			// does; until then a stopping one keeps its stop's record, as
			// ResumeReserves does.
			from := s.now
			if h.State == FleetStopping {
				from = maxTime(from, h.until)
			} else {
				h.StoppedAt = nil
			}
			h.startAt = from.Add(launchTick)
			switch {
			case h.State == FleetImageSaved || h.State == FleetStopping && *h.ReserveMode == ReserveHibernate:
				h.until = from.Add(simResume)
			case h.GPU != "":
				h.until = from.Add(s.p.Starts.GPUResume)
			default:
				h.until = from.Add(s.p.Starts.Resume)
			}
			h.State, h.ReserveMode, h.resumed, h.unbilledFrom = FleetStarting, nil, true, time.Time{}
		case ActionReturnToReserve:
			h := s.host(*a.Host)
			s.pending = append(s.pending, s.evict(h, func(HostCapacity) bool { return false })...)
			h.State, h.ReserveMode, h.Slept = FleetPreparing, a.Mode, true
			h.stopAt(s.now.Add(simStop))
			h.HandsBack = false
			h.offer.StoppedMicros = rootDiskMicros(h.Region, rootVolumeGiB, baselineMiBps)
		case ActionDrain:
			s.pending = append(s.pending, s.evict(s.host(*a.Host), func(HostCapacity) bool { return false })...)
			s.host(*a.Host).State, s.host(*a.Host).HandsBack = FleetDraining, false
		case ActionHandBack, ActionLend:
			s.host(*a.Host).HandsBack = a.Kind == ActionHandBack
		case ActionCancel:
			s.record(s.host(*a.Host).walk)
			s.hosts = slices.DeleteFunc(s.hosts, func(h *simHost) bool { return h.ID == *a.Host })
			s.unwait(*a.Host)
		case ActionRetireReserve:
			if h := s.host(*a.Host); h.walk != nil {
				s.record(h.walk)
			}
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

func (s *sim) account() {
	secs := int64(s.tick / time.Second)
	window := s.now.Before(s.until)
	for _, h := range s.hosts {
		if !s.billed(h) {
			s.r.spendMicros += h.offer.StoppedMicros * secs
			if window {
				s.r.windowMicros += h.offer.StoppedMicros * secs
			}
			continue
		}
		if window {
			s.r.windowMicros += h.offer.HourlyMicros * secs
			s.r.fleetCPUSec += float64(h.Usable.CPUMillis) / 1000 * float64(secs)
		}
		s.r.spendMicros += h.offer.HourlyMicros * secs
	}
}

// bill records what a platform container that ran for d bills at the rate
// card: its placement's rate class, CPU, memory and its host's cards.
func (s *sim) bill(c *simContainer, d time.Duration) {
	class := billing.RateClassFor(c.need.Region != "" || c.need.Zone != "", c.need.Preemptible)
	i := slices.IndexFunc(s.in.Rates, func(r billing.ComputeRate) bool {
		return r.Class == class && string(r.GPU) == c.market.GPU && r.Owner == billing.OwnerPlatformFleet
	})
	if i < 0 {
		s.t.Fatalf("no %s rate for GPU %q", class, c.market.GPU)
	}
	rate := s.in.Rates[i]
	perHour := int64(c.need.CPUMillis)*rate.CPUHour/1000 + c.need.MemoryBytes*rate.MemoryGiBHour/gib + int64(c.need.GPUsNeeded())*rate.GPUCardHour
	s.r.revenueNanos += perHour * d.Milliseconds() / int64(time.Hour/time.Millisecond)
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
