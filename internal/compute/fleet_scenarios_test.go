package compute

import (
	"cmp"
	"encoding/json"
	"fmt"
	"maps"
	"os"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/cpu"
)

// The scenarios run PlanFleet against a fleet simulation calibrated to the
// recorded prod run (testdata/fleetrun): measured lifecycle timings,
// placement oldest first and rate-card revenue. They print what each policy
// spends and how long work waits (go test -v) and assert invariants; the
// replay guard holds the simulation to what prod recorded.

const (
	simTick = time.Second
	// simLongTick steps scenarios that idle for hours.
	simLongTick = 10 * time.Second
	// From prod's host lifecycle logs of 2026-10-09: a launch serves its
	// first container 21-25 s after the purchase, a hibernated reserve
	// resumes to its first container in 9-13 s and a stopped one in 13-15 s,
	// and a bought reserve stops about 170 s after the purchase. simStop,
	// how long a serving host takes to stop into the reserve, is assumed.
	simProvision = 23 * time.Second
	simResume    = 12 * time.Second
	simBoot      = 15 * time.Second
	simPrepare   = 147 * time.Second
	simStop      = time.Minute
	// simCooldown is how long a refusal cools its pool, the fleet's
	// default CapacityCooldown.
	simCooldown = 10 * time.Minute
)

type simHost struct {
	FleetHost
	offer FleetOffer

	until      time.Time
	containers []*simContainer
	// served is set once a container ran on the host.
	served bool
	// failAt, when set, is when the host fails without serving, as one whose
	// bootstrap timed out.
	failAt *time.Time
}

type simContainer struct {
	id      uuid.UUID
	need    Requirement
	arrived time.Time
	runs    time.Duration
	ends    time.Time
	host    *HostID
	build   bool
	// market is the reserve market its placement counts in: its GPU
	// model's, else its CPU market.
	market ReserveMarket
	// fn names the work; started is when it was placed.
	fn      string
	started time.Time
}

type simArrival struct {
	at    time.Duration
	need  Requirement
	count int
	runs  time.Duration
	// build marks an image build, which keeps a warm slot of its shape in
	// its market for the policy's BuildWindow.
	build bool
	fn    string
}

type simResult struct {
	spendMicros, reserveMicros int64
	hours                      float64
	waits                      []time.Duration
	launches, stops, resumes   int
	refusals                   int
	// launched, refused, reserveBought and retired are when each launch,
	// refused launch, reserve purchase and retirement happened.
	launched, refused, reserveBought, retired []time.Time
	// unused counts serving hosts that left without running a container;
	// onDemand counts on-demand hosts bought to serve; mostHosts is the most
	// hosts that served at once.
	unused, onDemand, mostHosts int
	// pinnedWaits are the waits of work that cannot run on Spot; mostWarm
	// is the largest warm target the Spot market kept.
	pinnedWaits []time.Duration
	mostWarm    FleetCapacity
	finalHourly int64
	violations  []string
	// fnWaits are each function's start waits, retries apart.
	fnWaits map[string][]time.Duration
	// revenueNanos is what finished containers bill.
	revenueNanos int64
	// windowMicros is spend within the measure window, in µ$·s, and
	// fleetCPUSec running hosts' CPU seconds there.
	windowMicros int64
	fleetCPUSec  float64
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
	// quotas are EC2's vCPU quotas; the planner reads them only with
	// knowQuota.
	quotas    map[QuotaKey]int64
	knowQuota bool
	// recent is the largest recent shape per market the planner reads.
	recent map[ReserveMarket]FleetCapacity
	// floorShortSince carries each market's reserve shortfall from one pass
	// to the next, as the published plan does.
	floorShortSince map[ReserveMarket]time.Time
	peaks           map[ReserveMarket]LoadPeak
	// mostReserves is the most reserves any market held after a pass.
	mostReserves map[ReserveMarket]int
	// maxHosts is the fleet limit; tick is the step, simTick unless a long
	// scenario sets it; provision is how long a launch takes to serve.
	maxHosts  int
	tick      time.Duration
	provision time.Duration
	// refuses names the pools EC2 refuses for capacity. A refused launch
	// cools its zone and moves to the pool fallbackPool picks, as the
	// launcher does.
	refuses func(region, zoneID, instanceType string, market Market) bool
	// prices, when set, are the Spot quotes in force at a time.
	prices func(time.Time) []SpotQuote
	// until ends the window spend and capacity are measured over; zero is
	// the whole run.
	until time.Time
	// builds are the builds that ended, for the planner's build window, and
	// placedLog every container placed.
	builds    []simBuild
	placedLog []*simContainer
	next      int
}

type simBuild struct {
	at     time.Time
	market ReserveMarket
	shape  FleetCapacity
}

func spotSnapshot(t *testing.T, now time.Time, networks map[string]Network) []SpotQuote {
	t.Helper()
	raw, err := os.ReadFile("testdata/fleet/spot_prices.json")
	if err != nil {
		t.Fatal(err)
	}
	var snapshot struct {
		Quotes []struct {
			Region       string `json:"region"`
			ZoneID       string `json:"zone_id"`
			InstanceType string `json:"instance_type"`
			HourlyMicros int64  `json:"hourly_micros"`
		} `json:"quotes"`
	}
	if err := json.Unmarshal(raw, &snapshot); err != nil {
		t.Fatal(err)
	}
	var quotes []SpotQuote
	for _, q := range snapshot.Quotes {
		if _, ok := networks[q.Region]; ok {
			quotes = append(quotes, SpotQuote{Region: q.Region, ZoneID: q.ZoneID, InstanceType: q.InstanceType, HourlyMicros: q.HourlyMicros, ObservedAt: now})
		}
	}
	return quotes
}

func newSim(t *testing.T, p Policy) *sim {
	networks := map[string]Network{"us-east-2": {Subnets: []Subnet{
		{ID: "a", Zone: "us-east-2a", ZoneID: "use2-az1"}, {ID: "b", Zone: "us-east-2b", ZoneID: "use2-az2"}, {ID: "c", Zone: "us-east-2c", ZoneID: "use2-az3"},
	}}}
	return &sim{
		t: t, p: p, now: offerNow, start: offerNow, maxHosts: 40, tick: simTick, provision: simProvision,
		in: OfferInputs{Now: offerNow, Catalog: FleetCatalog(), Networks: networks, Rates: fleetRates(t), Spot: spotSnapshot(t, offerNow, networks)},
	}
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
					s.pending = append(s.pending, &simContainer{id: uuid.New(), need: a.need, arrived: s.now, runs: a.runs, build: a.build, fn: a.fn})
				}
			}
		}
		s.place()
		// A pass that held purchases for arrivals runs again once they
		// settle, as the planner loop does.
		if wait := s.plan(); wait > 0 {
			tick := s.now
			s.now = s.now.Add(wait)
			s.plan()
			s.now = tick
		}
		s.account()
		s.now = s.now.Add(s.tick)
	}
	for _, h := range s.hosts {
		for _, c := range h.containers {
			s.bill(c, s.now.Sub(c.started))
		}
	}
	s.r.hours = d.Hours()
	for _, h := range s.hosts {
		if h.State == FleetStopped || h.State == FleetImageSaved {
			s.r.finalHourly += h.offer.StoppedMicros
		} else {
			s.r.finalHourly += h.offer.HourlyMicros
		}
	}
	return s.r
}

// advance moves hosts through their timed phases and ends containers.
func (s *sim) advance() {
	s.hosts = slices.DeleteFunc(s.hosts, func(h *simHost) bool { return h.failAt != nil && !s.now.Before(*h.failAt) })
	for _, h := range s.hosts {
		if h.until.After(s.now) {
			continue
		}
		switch {
		case h.State == FleetStarting:
			h.State, h.PhaseAt = FleetServing, s.now
		case h.State == FleetPreparing && *h.ReserveMode == ReserveHibernate:
			h.State, h.Current = FleetImageSaved, true
			s.r.stops++
		case h.State == FleetPreparing:
			h.State, h.Current = FleetStopped, true
			s.r.stops++
		}
	}
	for _, h := range s.hosts {
		h.containers = slices.DeleteFunc(h.containers, func(c *simContainer) bool {
			if c.ends.After(s.now) {
				return false
			}
			if c.build {
				s.builds = append(s.builds, simBuild{at: s.now, market: ReserveMarket{Preemptible: c.need.Preemptible}, shape: reservedShape(c.need)})
			}
			s.bill(c, c.runs)
			return true
		})
	}
	// A draining host that runs nothing terminates.
	s.hosts = slices.DeleteFunc(s.hosts, func(h *simHost) bool {
		gone := h.State == FleetDraining && len(h.containers) == 0
		if gone && !h.served {
			s.r.unused++
		}
		return gone
	})
	serving := 0
	for _, h := range s.hosts {
		if h.State == FleetServing {
			serving++
		}
	}
	s.r.mostHosts = max(s.r.mostHosts, serving)
}

// place puts pending containers on serving hosts as placement does: the
// oldest first, as PendingContainers orders them, each on the host
// ChooseHost picks. Containers that arrived in one tick keep their order.
func (s *sim) place() {
	slices.SortStableFunc(s.pending, func(a, b *simContainer) int { return a.arrived.Compare(b.arrived) })
	s.pending = slices.DeleteFunc(s.pending, func(c *simContainer) bool {
		var serving []*simHost
		var rooms []HostCapacity
		for _, h := range s.hosts {
			if h.State == FleetServing {
				serving, rooms = append(serving, h), append(rooms, s.load(h))
			}
		}
		i := ChooseHost(rooms, c.need, s.p.OnDemand.Warm.Floor)
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
		c.ends, c.started = s.now.Add(c.runs), s.now
		c.market = ReserveMarket{Preemptible: c.need.Preemptible}
		if c.need.GPUsNeeded() > 0 {
			c.market = ReserveMarket{GPU: best.offer.Type.GPU}
		}
		best.containers, best.served = append(best.containers, c), true
		s.placedLog = append(s.placedLog, c)
		wait := s.now.Sub(c.arrived)
		s.r.waits = append(s.r.waits, wait)
		if !c.need.Preemptible {
			s.r.pinnedWaits = append(s.r.pinnedWaits, wait)
		}
		if s.r.fnWaits == nil {
			s.r.fnWaits = map[string][]time.Duration{}
		}
		s.r.fnWaits[c.fn] = append(s.r.fnWaits[c.fn], wait)
		return true
	})
}

func (s *sim) load(h *simHost) HostCapacity {
	fh := s.fleetHost(h)
	return fh.capacity()
}

func (s *sim) fleetHost(h *simHost) FleetHost {
	fh := h.FleetHost
	fh.Load, fh.Lent, fh.Containers, fh.BusySince = FleetCapacity{}, FleetCapacity{}, len(h.containers), nil
	var tolerant FleetCapacity
	for _, c := range h.containers {
		if fh.BusySince == nil || c.started.After(*fh.BusySince) {
			fh.BusySince = ptr(c.started)
		}
		fh.Load = fh.Load.Plus(reservedShape(c.need))
		if c.need.Preemptible {
			tolerant = tolerant.Plus(reservedShape(c.need))
		}
	}
	fh.Lent = lent(h.Market, h.Usable.GPUs, tolerant)
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
	var builds map[ReserveMarket]FleetCapacity
	for _, b := range s.builds {
		if s.now.Sub(b.at) < s.p.BuildWindow {
			if builds == nil {
				builds = map[ReserveMarket]FleetCapacity{}
			}
			builds[b.market] = builds[b.market].Upper(b.shape)
		}
	}
	// Arrived is what the Spot CPU market's placed work created within the
	// arrival window reserves, less its largest batch, as RecentShapes reads
	// it: a run is arrivals less than the batch's quiet apart, cut into
	// batches of its max from the run's first; older containers drop out of
	// the log.
	s.placedLog = slices.DeleteFunc(s.placedLog, func(c *simContainer) bool { return s.now.Sub(c.arrived) >= s.p.ArrivalWindow })
	var spot []*simContainer
	for _, c := range s.placedLog {
		if c.need.Preemptible && c.need.GPUsNeeded() == 0 {
			spot = append(spot, c)
		}
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
	if s.prices != nil {
		s.in.Spot = s.prices(s.now)
	}
	snapshot := FleetSnapshot{Now: s.now, Recent: s.recent, Builds: builds, Arrived: arrived, Offers: s.in, BatchWait: s.batchWait()}
	snapshot.Offers.Now = s.now
	// The scheduler refreshes Spot quotes far more often than they age out.
	snapshot.Offers.Spot = slices.Clone(s.in.Spot)
	for i := range snapshot.Offers.Spot {
		snapshot.Offers.Spot[i].ObservedAt = s.now
	}
	if s.knowQuota {
		for key, vcpus := range s.quotas {
			snapshot.Offers.Quotas = append(snapshot.Offers.Quotas, VCPUQuota{Key: key, VCPUs: vcpus})
		}
	}
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
		groups[key].Containers = append(groups[key].Containers, PendingContainer{ID: c.id, Host: c.host})
	}
	for _, key := range order {
		snapshot.Pending = append(snapshot.Pending, *groups[key])
	}
	snapshot.FloorShortSince, snapshot.Peaks = s.floorShortSince, s.peaks
	plan := PlanFleet(s.p, snapshot)
	s.floorShortSince, s.peaks = map[ReserveMarket]time.Time{}, map[ReserveMarket]LoadPeak{}
	for _, mp := range plan.Markets {
		if mp.Market == (ReserveMarket{Preemptible: true}) {
			s.r.mostWarm = s.r.mostWarm.Upper(mp.WarmTarget)
		}
		s.peaks[mp.Market] = mp.Peak
		if mp.FloorShortSince != nil {
			s.floorShortSince[mp.Market] = *mp.FloorShortSince
		}
	}
	s.check(snapshot, plan)
	s.apply(plan)
	held := map[ReserveMarket]int{}
	for _, h := range s.hosts {
		if h.reserve() {
			held[h.market()]++
		}
	}
	if s.mostReserves == nil {
		s.mostReserves = map[ReserveMarket]int{}
	}
	for m, n := range held {
		s.mostReserves[m] = max(s.mostReserves[m], n)
	}
	return plan.BatchWait
}

// check asserts no purchase serves a container a ready reserve would fit,
// and, since the simulation never refuses on-demand capacity, that each
// on-demand purchase takes its type's cheapest pool.
func (s *sim) check(snapshot FleetSnapshot, plan FleetPlan) {
	// A cheaper pool may lack the quota room the hosts and the pass's
	// earlier starts left.
	quotaUsed := QuotaUse(snapshot.Hosts, s.in.Catalog)
	used := maps.Clone(quotaUsed)
	for _, a := range plan.Actions {
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
		if reserve {
			cost = reserveCost(s.p)
		}
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
			class, _ := QuotaClassOf(h.InstanceType)
			key := QuotaKey{Region: h.Region, Class: class, Market: h.Market}
			limit, known := s.quotas[key]
			typ, _ := CatalogTypeNamed(h.InstanceType)
			full := s.knowQuota && known && quotaUsed[key]+typ.VCPUs() > limit
			if !h.resumable() || resumed[h.ID] || full {
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

func (s *sim) host(id HostID) *simHost {
	i := slices.IndexFunc(s.hosts, func(h *simHost) bool { return h.ID == id })
	if i < 0 {
		s.t.Fatalf("no host %s", id)
	}
	return s.hosts[i]
}

// launch starts a host for a purchase. EC2 refuses it when it would exceed
// its vCPU quota, cooling the offer or with quotas known its whole class,
// or when refuses names its pool, cooling the pool's zone; a refused launch
// moves on to the pool fallbackPool picks, up to maxLaunchPools.
func (s *sim) launch(a FleetAction, reserve bool, waiters []HostWaitersRow) (HostID, bool) {
	o := *a.Offer
	row := ClaimLaunchesRow{
		Market: ptr(string(o.Market)), GpuType: o.Type.GPU, GpuCount: int32(o.Type.GPUCount),
		CpuMillis: o.Usable.CPUMillis, MemoryBytes: o.Usable.MemoryBytes,
		HoldsCpuMillis: ptr(a.Holds.CPUMillis), HoldsMemoryBytes: ptr(a.Holds.MemoryBytes),
	}
	if a.Kind == ActionRightsize {
		row.Replaces, row.ReplacesHourlyMicros = ptr(uuid.UUID(*a.Host)), ptr(s.host(*a.Host).offer.HourlyMicros)
	}
	if reserve {
		row.ReserveMode = ptr(string(*a.Mode))
	}
	for pools := 0; ; pools++ {
		cool := OfferCooldown{Region: o.Region, InstanceType: o.Type.Name, Market: o.Market, Until: s.now.Add(simCooldown)}
		if limit, ok := s.quotas[o.Quota]; ok && s.quotaUse()[o.Quota]+o.Type.VCPUs() > limit {
			cool.Quota = s.knowQuota
		} else if s.refuses != nil && s.refuses(o.Region, o.ZoneID, o.Type.Name, o.Market) {
			cool.ZoneID = o.ZoneID
		} else {
			break
		}
		s.r.refusals++
		s.r.refused = append(s.r.refused, s.now)
		s.in.Cooldowns = append(s.in.Cooldowns, cool)
		next, ok := FleetOffer{}, false
		if pools+1 < maxLaunchPools {
			in := s.in
			in.Now = s.now
			next, ok = fallbackPool(s.p, row, waiters, in, func(string, bool) bool { return true })
		}
		if !ok {
			if row.Replaces != nil {
				s.host(*a.Host).RightsizeRefusedAt = ptr(s.now)
			}
			return HostID{}, false
		}
		o = next
	}
	if a.Kind == ActionRightsize {
		if replaced := s.host(*a.Host); o.running() >= replaced.offer.running() {
			s.r.violations = append(s.r.violations, fmt.Sprintf("%s replaced %s with %s, which costs no less",
				s.now.Format(time.TimeOnly), replaced.offer.Key(), o.Key()))
		}
	}
	s.next++
	var replaces *HostID
	if a.Kind == ActionRightsize {
		replaces = a.Host
	}
	h := &simHost{offer: o, FleetHost: FleetHost{Replaces: replaces,
		ID: HostID{byte(s.next >> 8), byte(s.next), 0xf1}, InstanceType: o.Type.Name, Region: o.Region, Zone: o.Zone, ZoneID: o.ZoneID, Market: o.Market,
		GPU: o.Type.GPU, Usable: o.Usable, State: FleetStarting, Current: true, HourlyMicros: ptr(o.HourlyMicros),
		HibernationConfigured: reserve && o.Hibernate, Stoppable: reserve || o.Market == MarketOnDemand,
	}}
	h.until = s.now.Add(s.provision)
	if reserve {
		h.State, h.ReserveMode, h.Current, h.Slept = FleetPreparing, a.Mode, false, true
		h.until = s.now.Add(s.provision + simPrepare)
		s.r.reserveBought = append(s.r.reserveBought, s.now)
	}
	if !reserve && o.Market == MarketOnDemand {
		s.r.onDemand++
	}
	s.hosts = append(s.hosts, h)
	s.r.launches++
	s.r.launched = append(s.r.launched, s.now)
	return h.ID, true
}

func (s *sim) quotaUse() map[QuotaKey]int64 {
	var hosts []FleetHost
	for _, h := range s.hosts {
		hosts = append(hosts, h.FleetHost)
	}
	return QuotaUse(hosts, s.in.Catalog)
}

func (s *sim) apply(plan FleetPlan) {
	bought := map[int]HostID{}
	for i, a := range plan.Actions {
		switch a.Kind {
		case ActionBuy, ActionRightsize, ActionBuyReserve:
			var waiters []HostWaitersRow
			for _, w := range plan.Waits {
				if w.Action != nil && *w.Action == i {
					c := s.pending[slices.IndexFunc(s.pending, func(c *simContainer) bool { return c.id == w.Container })]
					waiters = append(waiters, HostWaitersRow{Preemptible: c.need.Preemptible})
				}
			}
			if id, ok := s.launch(a, a.Kind == ActionBuyReserve, waiters); ok {
				bought[i] = id
			}
		case ActionResume, ActionRefresh:
			h := s.host(*a.Host)
			h.until = s.now.Add(simBoot)
			if h.State == FleetImageSaved {
				h.until = s.now.Add(simResume)
			}
			h.State, h.ReserveMode = FleetStarting, nil
			s.r.resumes++
		case ActionReturnToReserve:
			h := s.host(*a.Host)
			h.State, h.ReserveMode, h.Current, h.Slept, h.until = FleetPreparing, a.Mode, false, true, s.now.Add(simStop)
			h.offer.StoppedMicros = rootDiskMicros(h.Region, rootVolumeGiB, baselineMiBps)
		case ActionDrain:
			s.host(*a.Host).State = FleetDraining
		case ActionRetireReserve:
			s.hosts = slices.DeleteFunc(s.hosts, func(h *simHost) bool { return h.ID == *a.Host })
			s.r.retired = append(s.r.retired, s.now)
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
	for _, h := range s.hosts {
		h.IdleSince = nil
		if since, ok := plan.IdleSince[h.ID]; ok {
			h.IdleSince = ptr(since)
		}
	}
}

func (s *sim) account() {
	secs := int64(s.tick / time.Second)
	window := s.now.Before(s.until)
	for _, h := range s.hosts {
		if h.State == FleetStopped || h.State == FleetImageSaved || h.State == FleetHibernateUnverified {
			s.r.spendMicros += h.offer.StoppedMicros * secs
			s.r.reserveMicros += h.offer.StoppedMicros * secs
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

// slow counts the starts that waited over 30 seconds.
func (r simResult) slow() int {
	n := 0
	for _, w := range r.waits {
		if w > 30*time.Second {
			n++
		}
	}
	return n
}

func (r simResult) row(name string) string {
	waits := slices.Clone(r.waits)
	slices.SortFunc(waits, cmp.Compare)
	var p50, p95 time.Duration
	if len(waits) > 0 {
		p50, p95 = waits[(len(waits)-1)/2], waits[(len(waits)*95+99)/100-1]
	}
	return fmt.Sprintf("%-34s $%7.3f/h  reserve $%6.3f/h  idle-end $%6.3f/h  waits>30s %3d/%-3d  p50 %5s p95 %5s  launches %3d refused %3d stops %3d resumes %3d",
		name, float64(r.spendMicros)/3600/1e6/r.hours, float64(r.reserveMicros)/3600/1e6/r.hours, float64(r.finalHourly)/1e6,
		r.slow(), len(waits), p50, p95, r.launches, r.refusals, r.stops, r.resumes)
}

// demandOnly is the baseline the scenarios compare the policy with: no
// headroom and no reserves.
func demandOnly() Policy {
	p := DefaultPolicy()
	p.Spot, p.OnDemand, p.GPU = MarketReserve{}, MarketReserve{}, nil
	return p
}

func cpuNeed(millis cpu.Millis, memGiB int64) Requirement {
	return Requirement{CPUMillis: millis, MemoryBytes: memGiB * gib}
}

func TestFleetScenarios(t *testing.T) {
	t.Parallel()
	burst := func(at time.Duration, need Requirement, count int) []simArrival {
		var out []simArrival
		for i := range count {
			out = append(out, simArrival{at: at + time.Duration(i%6)*10*time.Second, need: need, count: 1, runs: 10 * time.Minute})
		}
		return out
	}
	gpu := Requirement{GPUs: []string{"T4"}, GPUCount: 1, CPUMillis: 2000, MemoryBytes: 8 * gib}
	scenarios := []struct {
		name     string
		d        time.Duration
		arrivals []simArrival
	}{
		{"quiet day", 24 * time.Hour, nil},
		{"burst", 2 * time.Hour, burst(time.Hour, cpuNeed(1000, 2), 40)},
		{"reserve depletion", 3 * time.Hour, append(append(burst(time.Hour, cpuNeed(2000, 4), 12),
			burst(time.Hour+15*time.Minute, cpuNeed(2000, 4), 12)...), burst(time.Hour+30*time.Minute, cpuNeed(2000, 4), 12)...)},
		{"GPU burst", 2 * time.Hour, burst(time.Hour, gpu, 4)},
		{"4-8 CPU burst", 2 * time.Hour, append(burst(time.Hour, cpuNeed(4000, 16), 5), burst(time.Hour, cpuNeed(8000, 32), 3)...)},
	}
	for _, sc := range scenarios {
		for _, policy := range []struct {
			name string
			p    Policy
		}{{"default policy", DefaultPolicy()}, {"demand only", demandOnly()}} {
			s := newSim(t, policy.p)
			if sc.d >= 6*time.Hour {
				s.tick = simLongTick
			}
			r := s.run(sc.d, sc.arrivals)
			t.Log(r.row(sc.name + ", " + policy.name))
			for _, v := range r.violations {
				t.Errorf("%s, %s: %s", sc.name, policy.name, v)
			}
			if want := len(sc.arrivals); len(r.waits) != want {
				t.Errorf("%s, %s: %d of %d containers placed", sc.name, policy.name, len(r.waits), want)
			}
		}
	}
}

// TestFleetSpendAtZeroLoadIsTheFloorsCost checks a quiet fleet settles on
// exactly what an empty fleet buys for the floors once they are due.
func TestFleetSpendAtZeroLoadIsTheFloorsCost(t *testing.T) {
	t.Parallel()
	s := newSim(t, DefaultPolicy())
	s.tick = simLongTick
	r := s.run(6*time.Hour, nil)
	first := PlanFleet(DefaultPolicy(), FleetSnapshot{Now: offerNow, Offers: s.in, HostRoom: s.maxHosts, ReserveRoom: s.maxHosts})
	var want int64
	for _, a := range first.Actions {
		switch a.Kind {
		case ActionBuy:
			want += a.Offer.HourlyMicros
		case ActionBuyReserve:
			want += a.Offer.StoppedMicros
		case ActionResume, ActionReturnToReserve, ActionDrain, ActionRetireReserve, ActionRightsize, ActionRefresh:
			t.Fatalf("an empty fleet's first pass %s", a.Kind)
		}
	}
	if r.finalHourly != want {
		t.Fatalf("quiet fleet costs %d µ$/h, the floors' cover %d", r.finalHourly, want)
	}
	t.Logf("floors cost $%.4f/h ($%.0f/month): %d hosts", float64(want)/1e6, float64(want)*730/1e6, len(first.Actions))
	for _, a := range first.Actions {
		t.Logf("  %s %s %s", a.Kind, a.Market, a.Offer.Key())
	}
}

// TestFleetQuotaScenario is Oregon with a G Spot quota of 0, where
// Spot-tolerant T4 work can only use Spot. Without the quota every pass
// tries another G type and is refused; with it nothing is tried.
func TestFleetQuotaScenario(t *testing.T) {
	t.Parallel()
	need := Requirement{Preemptible: true, GPUs: []string{"T4"}, GPUCount: 1, CPUMillis: 2000, MemoryBytes: 8 * gib}
	arrivals := []simArrival{{at: 10 * time.Minute, need: need, count: 2, runs: 10 * time.Minute}}
	for _, known := range []bool{false, true} {
		s := newSim(t, DefaultPolicy())
		s.in.Networks = map[string]Network{"us-west-2": oneZone("us-west-2a", "usw2-az1")}
		s.in.Spot = spotSnapshot(t, offerNow, s.in.Networks)
		s.quotas = map[QuotaKey]int64{{Region: "us-west-2", Class: QuotaG, Market: MarketSpot}: 0}
		s.knowQuota = known
		r := s.run(time.Hour, arrivals)
		name := "Oregon G Spot quota 0, quota unknown"
		if known {
			name = "Oregon G Spot quota 0, quota read"
		}
		t.Log(r.row(name))
		if known && r.refusals > 0 {
			t.Errorf("%s: %d refused launches", name, r.refusals)
		}
		if !known && r.refusals == 0 {
			t.Errorf("%s: the scenario did not reach the quota", name)
		}
	}
}

// Each CPU market keeps a floor reserve and one beside it that fits its
// largest recent shape, and each GPU model work used keeps one, through a
// burst of 8 CPU work and the quiet hours after; a model nobody used keeps
// none.
func TestReservesHoldALargeHostPerMarketAndOnePerUsedGPUModel(t *testing.T) {
	s := newSim(t, DefaultPolicy())
	s.recent = map[ReserveMarket]FleetCapacity{{GPU: "T4"}: {CPUMillis: 2000, MemoryBytes: 8 * gib, GPUs: 1}}
	eight := cpuNeed(8_000, 16)
	var arrivals []simArrival
	for _, preemptible := range []bool{false, true} {
		need := eight
		need.Preemptible = preemptible
		arrivals = append(arrivals, simArrival{at: time.Hour, need: need, count: 2, runs: 10 * time.Minute})
	}
	r := s.run(6*time.Hour, arrivals)
	if len(r.waits) != 4 || len(r.violations) > 0 {
		t.Fatalf("placed %d of 4, violations %v", len(r.waits), r.violations)
	}
	want := map[ReserveMarket]int{{Preemptible: true}: 2, {}: 2, {GPU: "T4"}: 1}
	held := map[ReserveMarket][]string{}
	large := map[ReserveMarket]bool{}
	for _, h := range s.hosts {
		if !h.reserve() {
			continue
		}
		held[h.market()] = append(held[h.market()], h.InstanceType)
		large[h.market()] = large[h.market()] || h.Usable.Covers(reservedShape(eight))
	}
	for m, n := range want {
		// A host returning from the burst may briefly sit beside the
		// reserves it replaces before the surplus retires.
		if len(held[m]) != n || s.mostReserves[m] > n+1 || (m.GPU == "" && !large[m]) {
			t.Errorf("%s holds %v and held up to %d; want %d, one that fits 8 CPU", m, held[m], s.mostReserves[m], n)
		}
	}
	if len(held) != len(want) {
		t.Errorf("reserves %v", held)
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

// shortSpot names the cheaper half of each type's quoted Spot pools, as
// Spot capacity runs out first where it is cheapest.
func shortSpot(quotes []SpotQuote) map[string]bool {
	byType := map[string][]SpotQuote{}
	for _, q := range quotes {
		byType[q.InstanceType] = append(byType[q.InstanceType], q)
	}
	short := map[string]bool{}
	for _, qs := range byType {
		slices.SortFunc(qs, func(a, b SpotQuote) int { return cmp.Compare(a.HourlyMicros, b.HourlyMicros) })
		for _, q := range qs[:(len(qs)+1)/2] {
			short[q.Region+"/"+q.ZoneID+"/"+q.InstanceType] = true
		}
	}
	return short
}

// newProdSim is the platform fleet in its four regions; with short set,
// EC2 refuses the Spot pools shortSpot names, and without it those pools
// are not sold at all.
func newProdSim(t *testing.T, short bool) *sim {
	s := newSim(t, DefaultPolicy())
	s.in.Networks = prodNetworks()
	s.in.Spot = spotSnapshot(t, offerNow, s.in.Networks)
	refused := shortSpot(s.in.Spot)
	if short {
		s.refuses = func(region, zoneID, instanceType string, market Market) bool {
			return market == MarketSpot && refused[region+"/"+zoneID+"/"+instanceType]
		}
	} else {
		s.in.Spot = slices.DeleteFunc(s.in.Spot, func(q SpotQuote) bool { return refused[q.Region+"/"+q.ZoneID+"/"+q.InstanceType] })
	}
	return s
}

// A day of builds, a burst and a large job with Spot capacity short in the
// cheaper half of each type's pools: the fleet spends about what it would
// were those pools never sold, a rightsize never swaps a host for one that
// costs no less, and once work stops the fleet stops launching and stops
// trying refused pools.
func TestFleetRefusalsCostAboutWhatThePoolsAbsenceDoes(t *testing.T) {
	t.Parallel()
	build := cpuNeed(4000, 8)
	build.Preemptible = true
	arrivals := []simArrival{
		{at: 30 * time.Minute, need: build, count: 1, runs: 3 * time.Minute, build: true},
		{at: time.Hour, need: cpuNeed(1000, 2), count: 12, runs: 10 * time.Minute},
		{at: 2 * time.Hour, need: cpuNeed(12000, 24), count: 1, runs: 5 * time.Minute},
		{at: 3 * time.Hour, need: build, count: 1, runs: 3 * time.Minute, build: true},
	}
	const day, quiet = 8 * time.Hour, 5 * time.Hour
	results := map[bool]simResult{}
	for _, short := range []bool{true, false} {
		s := newProdSim(t, short)
		s.tick = simLongTick
		r := s.run(day, arrivals)
		name := "four regions, cheaper Spot pools refused"
		if !short {
			name = "four regions, those pools unsold"
		}
		t.Log(r.row(name))
		for _, v := range r.violations {
			t.Errorf("%s: %s", name, v)
		}
		if want := 15; len(r.waits) != want {
			t.Errorf("%s: %d of %d containers placed", name, len(r.waits), want)
		}
		for _, at := range slices.Concat(r.launched, r.refused) {
			if at.Sub(offerNow) >= day-quiet+time.Hour {
				t.Errorf("%s: launched or was refused at %s, hours after the last work", name, at.Format(time.TimeOnly))
			}
		}
		results[short] = r
	}
	if short, unsold := results[true].spendMicros, results[false].spendMicros; short > unsold*105/100 {
		t.Errorf("refused pools cost $%.3f, their absence $%.3f", float64(short)/3600/1e6, float64(unsold)/3600/1e6)
	}
}

// A reserve woken by a build larger than the market's other reserves fit
// goes back into the reserve: once a cheaper host holds the build's warm
// slot, or when the slot lapses if none can. The market buys no other
// reserve meanwhile, retires none, and ends with the reserves it began
// with.
func TestAReserveWokenByABuildReturnsWithoutARebuy(t *testing.T) {
	t.Parallel()
	for _, cpus := range []cpu.Millis{6000, 12000} {
		s := newProdSim(t, false)
		s.tick = simLongTick
		// A 16 CPU job ran this week, so each market's reserve fits 16 CPU.
		s.recent = map[ReserveMarket]FleetCapacity{{}: cpuGiB(16000, 32), {Preemptible: true}: cpuGiB(16000, 32)}
		build := cpuNeed(cpus, 12)
		build.Preemptible = true
		settled := time.Hour
		before := s.run(settled, nil)
		reserves := func() []string {
			var out []string
			for _, h := range s.hosts {
				if h.reserve() {
					out = append(out, h.offer.Key())
				}
			}
			slices.Sort(out)
			return out
		}
		began := reserves()
		s.start, s.r = s.now, simResult{}
		r := s.run(3*time.Hour, []simArrival{{need: build, count: 1, runs: 3 * time.Minute, build: true}})
		if len(r.waits) != 1 || len(r.violations) > 0 || len(before.violations) > 0 {
			t.Fatalf("%v CPU build: placed %d of 1, violations %v", cpus, len(r.waits), slices.Concat(before.violations, r.violations))
		}
		if r.resumes == 0 {
			t.Fatalf("%v CPU build woke no reserve", cpus)
		}
		if len(r.reserveBought) > 0 || len(r.retired) > 0 {
			t.Errorf("%v CPU build: bought reserves at %v, retired at %v", cpus, r.reserveBought, r.retired)
		}
		if ended := reserves(); !slices.Equal(ended, began) {
			t.Errorf("%v CPU build: reserves %v, began with %v", cpus, ended, began)
		}
	}
}

// Recurring bursts keep their reserves: in a market whose stopped target
// shares load, a burst's share holds the target for the cost horizon, so
// the next burst resumes those reserves rather than the market retiring
// and rebuying them. They retire once the horizon passes without a burst.
func TestRecurringBurstsKeepTheirReserves(t *testing.T) {
	need := cpuNeed(1000, 2)
	var arrivals []simArrival
	for _, at := range []time.Duration{time.Hour, 100 * time.Minute, 140 * time.Minute} {
		arrivals = append(arrivals, simArrival{at: at, need: need, count: 100, runs: 20 * time.Minute})
	}
	s := newProdSim(t, false)
	r := s.run(4*time.Hour, arrivals)
	t.Log(r.row("three bursts of 100 on-demand containers"))
	if len(r.waits) != 300 || len(r.violations) > 0 {
		t.Fatalf("placed %d of 300, violations %v", len(r.waits), r.violations)
	}
	if r.resumes == 0 {
		t.Fatal("no burst resumed a reserve")
	}
	lastPeak := offerNow.Add(145 * time.Minute)
	for _, at := range r.retired {
		if at.Before(lastPeak.Add(DefaultPolicy().CostHorizon)) {
			t.Errorf("retired a reserve at %s, within the cost horizon of a burst", at.Format(time.TimeOnly))
		}
	}
}

// spotRamp is n one-CPU Spot-tolerant containers arriving evenly over ten
// minutes, all running until minute twenty.
func spotRamp(n int) []simArrival {
	need := cpuNeed(1000, 2)
	need.Preemptible = true
	var out []simArrival
	for i := range 20 {
		at := time.Duration(i) * 30 * time.Second
		out = append(out, simArrival{at: time.Hour + at, need: need, count: n / 20, runs: 20*time.Minute - at})
	}
	return out
}

// A ramp of Spot-tolerant work runs on hosts that grow with the market,
// averaging at least half the largest shape, and starts warm: headroom
// follows the steady arrival rate, so beyond the ramp's first batches no
// start waits for a launch. Spot-tolerant work buys no on-demand host, and
// work that cannot run on Spot starts at once on the on-demand floor it
// keeps. Only the floor bought again for those starts and the headroom
// the market held when the ramp stopped may leave unused.
func TestASpotRampStartsWarmOnLargeHosts(t *testing.T) {
	t.Parallel()
	p := DefaultPolicy()
	for _, n := range []int{200, 400} {
		s := newProdSim(t, false)
		s.maxHosts, s.provision = 250, 45*time.Second
		pinned := []simArrival{
			{at: time.Hour + 4*time.Minute, need: cpuNeed(1000, 2), count: 1, runs: 5 * time.Minute},
			{at: time.Hour + 16*time.Minute, need: cpuNeed(1000, 2), count: 1, runs: 5 * time.Minute},
		}
		r := s.run(2*time.Hour, append(spotRamp(n), pinned...))
		t.Log(r.row(fmt.Sprintf("Spot ramp to %d", n)) + fmt.Sprintf("  most hosts %d unused %d on-demand %d", r.mostHosts, r.unused, r.onDemand))
		if len(r.waits) != n+len(pinned) || len(r.violations) > 0 {
			t.Fatalf("ramp to %d: placed %d, violations %v", n, len(r.waits), r.violations)
		}
		// Beside the two warm floors and their refills, hosts average at
		// least half the largest shape.
		largest := p.LargestShape.Cap.CPUMillis
		if hosts := r.mostHosts - 2 - len(pinned); cpu.Millis(n*1000) < cpu.Millis(hosts)*largest/2 {
			t.Errorf("ramp to %d ran on %d hosts", n, r.mostHosts)
		}
		// The first two batches arrive before any headroom can serve.
		if slow := r.slow(); slow > 2*n/20 {
			t.Errorf("ramp to %d: %d starts waited over 30s", n, slow)
		}
		if r.onDemand > 1+len(pinned) {
			t.Errorf("ramp to %d bought %d on-demand hosts", n, r.onDemand)
		}
		if spare := len(pinned) + int((r.mostWarm.CPUMillis+largest-1)/largest); r.unused > spare {
			t.Errorf("ramp to %d: %d hosts left without running a container, want at most %d", n, r.unused, spare)
		}
		for _, w := range r.pinnedWaits {
			if w > simTick {
				t.Errorf("ramp to %d: work that cannot run on Spot waited %s", n, w)
			}
		}
	}
}
