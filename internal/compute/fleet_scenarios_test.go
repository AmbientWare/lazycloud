package compute

import (
	"cmp"
	"encoding/json"
	"fmt"
	"os"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/cpu"
)

// The scenarios run PlanFleet against a small fleet simulation with fixed
// lifecycle timings, reviewed prices and the recorded Spot snapshot. They
// print what each policy spends and how long work waits (go test -v) and
// assert only invariants.

const (
	simTick      = 10 * time.Second
	simProvision = 300 * time.Second
	simPrepare   = 60 * time.Second
	simResume    = 30 * time.Second
	simBoot      = 120 * time.Second
	simMaxHosts  = 40
)

type simHost struct {
	FleetHost
	offer FleetOffer

	until      time.Time
	containers []*simContainer
}

type simContainer struct {
	id      uuid.UUID
	need    Requirement
	arrived time.Time
	runs    time.Duration
	ends    time.Time
	host    *HostID
}

type simArrival struct {
	at    time.Duration
	need  Requirement
	count int
	runs  time.Duration
}

type simResult struct {
	spendMicros, reserveMicros int64
	hours                      float64
	waits                      []time.Duration
	launches, stops, resumes   int
	refusals                   int
	finalHourly                int64
	violations                 []string
}

type sim struct {
	t       *testing.T
	p       Policy
	in      OfferInputs
	now     time.Time
	start   time.Time
	hosts   []*simHost
	pending []*simContainer
	next    byte
	r       simResult
	// quotas are EC2's vCPU quotas; the planner reads them only with
	// knowQuota.
	quotas    map[QuotaKey]int64
	knowQuota bool
	// recent is the largest recent shape per market the planner reads.
	recent map[ReserveMarket]FleetCapacity
	// floorShortSince carries each market's floor shortfall from one pass to
	// the next, as the published plan does.
	floorShortSince map[ReserveMarket]time.Time
	// mostReserves is the most reserves any market held after a pass.
	mostReserves map[ReserveMarket]int
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
		t: t, p: p, now: offerNow, start: offerNow,
		in: OfferInputs{Now: offerNow, Catalog: FleetCatalog(), Networks: networks, Rates: fleetRates(t), Spot: spotSnapshot(t, offerNow, networks)},
	}
}

func (s *sim) run(d time.Duration, arrivals []simArrival) simResult {
	for s.now.Before(s.start.Add(d)) {
		s.advance()
		for _, a := range arrivals {
			if at := s.start.Add(a.at); !at.Before(s.now) && at.Before(s.now.Add(simTick)) {
				for range a.count {
					s.pending = append(s.pending, &simContainer{id: uuid.New(), need: a.need, arrived: s.now, runs: a.runs})
				}
			}
		}
		s.place()
		s.plan()
		s.account()
		s.now = s.now.Add(simTick)
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

func needShape(r Requirement) FleetCapacity {
	return FleetCapacity{CPUMillis: r.CPUMillis, MemoryBytes: r.MemoryBytes, GPUs: r.GPUsNeeded()}
}

// advance moves hosts through their timed phases and ends containers.
func (s *sim) advance() {
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
		h.containers = slices.DeleteFunc(h.containers, func(c *simContainer) bool { return !c.ends.After(s.now) })
	}
	s.hosts = slices.DeleteFunc(s.hosts, func(h *simHost) bool { return h.State == FleetDraining && len(h.containers) == 0 })
}

// place puts pending containers on serving hosts, best fit.
func (s *sim) place() {
	slices.SortStableFunc(s.pending, func(a, b *simContainer) int { return size(b.need) - size(a.need) })
	s.pending = slices.DeleteFunc(s.pending, func(c *simContainer) bool {
		var best *simHost
		for _, h := range s.hosts {
			if h.State != FleetServing || !s.load(h).Fits(c.need) {
				continue
			}
			if best == nil || s.load(h).FreeCPUMillis < s.load(best).FreeCPUMillis {
				best = h
			}
		}
		if best == nil {
			return false
		}
		c.ends = s.now.Add(c.runs)
		best.containers = append(best.containers, c)
		s.r.waits = append(s.r.waits, s.now.Sub(c.arrived))
		return true
	})
}

func (s *sim) load(h *simHost) HostCapacity {
	fh := s.fleetHost(h)
	return fh.capacity()
}

func (s *sim) fleetHost(h *simHost) FleetHost {
	fh := h.FleetHost
	fh.Load, fh.Containers = FleetCapacity{}, len(h.containers)
	for _, c := range h.containers {
		fh.Load = fh.Load.Plus(needShape(c.need))
	}
	return fh
}

func (s *sim) plan() {
	snapshot := FleetSnapshot{Now: s.now, Recent: s.recent, Offers: s.in}
	snapshot.Offers.Now = s.now
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
	snapshot.HostRoom, snapshot.ReserveRoom = simMaxHosts-live, simMaxHosts-reserves
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
	snapshot.FloorShortSince = s.floorShortSince
	plan := PlanFleet(s.p, snapshot)
	s.floorShortSince = map[ReserveMarket]time.Time{}
	for _, mp := range plan.Markets {
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
}

// check asserts no purchase serves a container a ready reserve would fit.
func (s *sim) check(snapshot FleetSnapshot, plan FleetPlan) {
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

func (s *sim) host(id HostID) *simHost {
	i := slices.IndexFunc(s.hosts, func(h *simHost) bool { return h.ID == id })
	if i < 0 {
		s.t.Fatalf("no host %s", id)
	}
	return s.hosts[i]
}

// launch starts a host for a purchase, or refuses it as EC2 would when the
// purchase exceeds its vCPU quota; a refusal cools the offer, or with
// quotas known its whole class.
func (s *sim) launch(a FleetAction, reserve bool) (HostID, bool) {
	o := *a.Offer
	if limit, ok := s.quotas[o.Quota]; ok && s.quotaUse()[o.Quota]+o.Type.VCPUs > limit {
		s.r.refusals++
		s.in.Cooldowns = append(s.in.Cooldowns, OfferCooldown{
			Region: o.Region, InstanceType: o.Type.Name, Market: o.Market, RefusedAt: s.now, Until: s.now.Add(10 * time.Minute), Quota: s.knowQuota,
		})
		return HostID{}, false
	}
	s.next++
	h := &simHost{offer: o, FleetHost: FleetHost{
		ID: HostID{s.next, 0xf1}, InstanceType: o.Type.Name, Region: o.Region, Zone: o.Zone, ZoneID: o.ZoneID, Market: o.Market,
		GPU: o.Type.GPU, Usable: o.Usable, State: FleetStarting, Current: true, HourlyMicros: ptr(o.HourlyMicros),
		HibernationConfigured: reserve && o.Hibernate, Stoppable: reserve || o.Market == MarketOnDemand,
	}}
	h.until = s.now.Add(simProvision)
	if reserve {
		h.State, h.ReserveMode, h.Current = FleetPreparing, a.Mode, false
		h.until = s.now.Add(simProvision + simPrepare)
	}
	s.hosts = append(s.hosts, h)
	s.r.launches++
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
			if id, ok := s.launch(a, a.Kind == ActionBuyReserve); ok {
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
			h.State, h.ReserveMode, h.Current, h.until = FleetPreparing, a.Mode, false, s.now.Add(simPrepare)
			h.offer.StoppedMicros = rootDiskMicros(h.Region, rootVolumeGiB)
		case ActionDrain:
			s.host(*a.Host).State = FleetDraining
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
	for _, h := range s.hosts {
		h.IdleSince = nil
		if since, ok := plan.IdleSince[h.ID]; ok {
			h.IdleSince = ptr(since)
		}
	}
}

func (s *sim) account() {
	secs := int64(simTick / time.Second)
	for _, h := range s.hosts {
		if h.State == FleetStopped || h.State == FleetImageSaved || h.State == FleetHibernateUnverified {
			s.r.spendMicros += h.offer.StoppedMicros * secs
			s.r.reserveMicros += h.offer.StoppedMicros * secs
			continue
		}
		s.r.spendMicros += h.offer.HourlyMicros * secs
	}
}

func (r simResult) row(name string) string {
	waits := slices.Clone(r.waits)
	slices.SortFunc(waits, cmp.Compare)
	var p95 time.Duration
	long := 0
	for _, w := range waits {
		if w > 30*time.Second {
			long++
		}
	}
	if len(waits) > 0 {
		p95 = waits[(len(waits)*95+99)/100-1]
	}
	return fmt.Sprintf("%-34s $%7.3f/h  reserve $%6.3f/h  idle-end $%6.3f/h  waits>30s %3d/%-3d  p95 %5s  launches %3d refused %3d stops %3d resumes %3d",
		name, float64(r.spendMicros)/3600/1e6/r.hours, float64(r.reserveMicros)/3600/1e6/r.hours, float64(r.finalHourly)/1e6,
		long, len(waits), p95, r.launches, r.refusals, r.stops, r.resumes)
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
	burst := func(at time.Duration, need Requirement, count int) []simArrival {
		var out []simArrival
		for i := range count {
			out = append(out, simArrival{at: at + time.Duration(i%6)*simTick, need: need, count: 1, runs: 10 * time.Minute})
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
			r := newSim(t, policy.p).run(sc.d, sc.arrivals)
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
	s := newSim(t, DefaultPolicy())
	r := s.run(6*time.Hour, nil)
	due := offerNow.Add(-time.Hour)
	first := PlanFleet(DefaultPolicy(), FleetSnapshot{Now: offerNow, Offers: s.in, HostRoom: simMaxHosts, ReserveRoom: simMaxHosts,
		FloorShortSince: map[ReserveMarket]time.Time{{}: due, {Preemptible: true}: due}})
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

// TestFleetQuotaScenario is Oregon with a G Spot quota of 0, as on
// 2026-09-18: Spot-tolerant T4 work can only use Spot there. Without the
// quota every pass tries another G type and is refused; with it nothing is
// tried.
func TestFleetQuotaScenario(t *testing.T) {
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
