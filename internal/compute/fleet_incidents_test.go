package compute

import (
	"fmt"
	"slices"
	"testing"
	"time"
)

// The incident scenarios replay what prod hit after a deploy, through the
// simulation's launcher walks, Spot request disables and deploys.

// l4Job is the Spot-tolerant L4 function prod ran on 2026-10-13.
var l4Job = Requirement{Preemptible: true, GPUs: []string{"L4"}, GPUCount: 1, CPUMillis: 2000, MemoryBytes: 13056 << 20}

// gpuSpot reports a Spot pool of a GPU type.
func gpuSpot(typ string, market Market) bool {
	c, _ := CatalogTypeNamed(typ)
	return market == MarketSpot && c.GPU != ""
}

// oversized names an offer bought or tried to hold holds that has more
// cards than a fitting type of its model, or that a smaller fitting type
// undercuts in its cheapest pool, cooldowns and quotas aside.
func oversized(p Policy, in OfferInputs, o FleetOffer, holds FleetCapacity) string {
	in.Cooldowns, in.Quotas = nil, nil
	need := Requirement{CPUMillis: holds.CPUMillis, MemoryBytes: holds.MemoryBytes, GPUCount: holds.GPUs, Preemptible: o.Market == MarketSpot}
	if o.Type.GPU != "" {
		need.GPUs = []string{o.Type.GPU}
	}
	cheapest := map[string]int64{}
	for _, c := range RankOffers(p, need, false, in) {
		if c.Market != o.Market {
			continue
		}
		if price, ok := cheapest[c.Type.Name]; !ok || c.HourlyMicros < price {
			cheapest[c.Type.Name] = c.HourlyMicros
		}
	}
	for name, price := range cheapest {
		t, _ := CatalogTypeNamed(name)
		switch {
		case t.GPUCount < o.Type.GPUCount:
			return fmt.Sprintf("%s has %d cards where %s has %d", o.Key(), o.Type.GPUCount, name, t.GPUCount)
		case name != o.Type.Name && o.Usable.Covers(t.Usable(0)) && price < o.HourlyMicros:
			return fmt.Sprintf("%s costs %d µ$/h where the smaller %s costs %d", o.Key(), o.HourlyMicros, name, price)
		}
	}
	return ""
}

// Work purchases follow the size rule: with every L4 Spot pool refusing, as
// on 2026-10-13, the 2 CPU / 12.75 GiB job neither buys nor walks to a
// g6.8xlarge, a 4-card g6.12xlarge or a g6.16xlarge, and it still starts.
func TestIncidentAnL4JobBuysNoLargerHostThanItNeeds(t *testing.T) {
	t.Parallel()
	s := newChurnSim(t)
	s.refuses = func(_, _, typ string, market Market) bool { return gpuSpot(typ, market) }
	s.tick = simLongTick
	s.run(30*time.Minute, nil)
	s.start, s.until, s.r = s.now, time.Time{}, simResult{}
	r := s.run(time.Hour, []simArrival{{need: l4Job, count: 1, runs: 10 * time.Minute, fn: "gpu"}})
	for _, p := range r.bought {
		if p.containers == 0 {
			continue
		}
		for _, o := range p.tried {
			if why := oversized(s.p, s.in, o, p.holds); why != "" {
				t.Errorf("%s: %s", p.at.Format(time.TimeOnly), why)
			}
		}
	}
	if len(r.fnWaits["gpu"]) != 1 || len(r.violations) > 0 {
		t.Errorf("placed %d of 1, violations %v", len(r.fnWaits["gpu"]), r.violations)
	}
}

// Every pool one launch walk refuses cools until one end, however many
// passes the walk takes, so its market buys again once rather than as each
// pool comes back. Over two hours of refused GPU Spot reserves, each
// market's purchases start at least a cooldown apart after a refusal.
func TestIncidentAWalksRefusedPoolsShareOneEnd(t *testing.T) {
	t.Parallel()
	s := newChurnSim(t)
	s.refuses = func(_, _, typ string, market Market) bool { return gpuSpot(typ, market) }
	s.tick = simLongTick
	r := s.run(2*time.Hour, nil)
	for _, v := range r.violations {
		t.Error(v)
	}
	last := map[ReserveMarket]simPurchase{}
	for _, p := range r.purchases {
		if prev, ok := last[p.market]; ok && !prev.launched && p.at.Sub(prev.at) < simCooldown {
			t.Errorf("%s bought again %s after a refused purchase", p.market, p.at.Sub(prev.at))
		}
		last[p.market] = p
	}
}

// A deploy that changes no host code leaves the reserves in place: with
// every GPU Spot pool refusing after the fleet settled, as on 2026-10-13,
// no reserve retires and the next L4 job resumes one.
func TestIncidentADeployWithoutAnAgentChangeKeepsTheReserves(t *testing.T) {
	t.Parallel()
	s := newChurnSim(t)
	s.tick = simLongTick
	s.run(time.Hour, nil)
	s.start, s.until, s.r = s.now, time.Time{}, simResult{}
	s.refuses = func(_, _, typ string, market Market) bool { return gpuSpot(typ, market) }
	s.deploys = []simDeploy{{at: time.Minute}}
	r := s.run(30*time.Minute, []simArrival{{at: 10 * time.Minute, need: l4Job, count: 1, runs: 5 * time.Minute, fn: "gpu"}})
	if len(r.retired) > 0 {
		t.Errorf("a deploy without an agent change retired reserves at %v", r.retired)
	}
	if r.starts[startResumed] != 1 || len(r.violations) > 0 {
		t.Errorf("the L4 job after the deploy: starts %v, violations %v", r.starts, r.violations)
	}
}

// A deploy that changes the agent replaces each reserve before it retires
// it: a stale reserve retires only once its market holds a ready reserve
// prepared on the new agent.
func TestIncidentADeployWithAnAgentChangeReplacesReservesFirst(t *testing.T) {
	t.Parallel()
	s := newChurnSim(t)
	s.tick = simLongTick
	s.run(time.Hour, nil)
	s.start, s.until, s.r = s.now, time.Time{}, simResult{}
	s.deploys = []simDeploy{{at: time.Minute, agent: true}}
	s.checkRetire = true
	r := s.run(time.Hour, nil)
	for _, v := range r.violations {
		t.Error(v)
	}
	if len(r.retired) == 0 {
		t.Error("no stale reserve retired")
	}
}

// EC2 holds a stopped Spot reserve's request active for up to 165 s. L4
// work that waits for the reserve keeps it: nothing retires the reserve,
// and the work starts once EC2 disables the request and the reserve
// resumes. A request active past the planner's bound buys the work a host
// instead, still retiring nothing.
func TestIncidentASlowSpotDisableKeepsTheReserve(t *testing.T) {
	t.Parallel()
	st := DefaultPolicy().Starts
	stopped := st.GPULaunch + simPrepare + simGPUStop
	for _, gap := range []time.Duration{146 * time.Second, 165 * time.Second, 10 * time.Minute} {
		s := newSim(t, DefaultPolicy())
		s.disableGap = gap
		at := stopped + 33*time.Second
		r := s.run(at+20*time.Minute, []simArrival{{at: at, need: l4Job, count: 1, runs: time.Minute, fn: "gpu"}})
		if r.stuckRetired > 0 || len(r.violations) > 0 {
			t.Errorf("request active %s: %d reserves retired, violations %v", gap, r.stuckRetired, r.violations)
		}
		waits := r.fnWaits["gpu"]
		if len(waits) != 1 {
			t.Errorf("request active %s: placed %d of 1", gap, len(waits))
			continue
		}
		// It waits for the reserve, or past the bound for a purchase.
		want := gap - 33*time.Second + st.GPUResume
		if gap > 3*time.Minute {
			want = 3*time.Minute - 33*time.Second + st.GPULaunch
		}
		if waits[0] > want+15*time.Second {
			t.Errorf("request active %s: the work waited %s, want about %s", gap, waits[0], want)
		}
		t.Logf("request active %s: waited %s, %d resumes, %d launches", gap, waits[0], r.resumes, r.launches)
	}
}

// retireViolation names a stale reserve retired while its market holds no
// ready reserve on the target release.
func (s *sim) retireViolation(h *simHost) {
	if s.current(h) {
		return
	}
	if !slices.ContainsFunc(s.hosts, func(o *simHost) bool { return o != h && o.market() == h.market() && o.resumable() && s.current(o) }) {
		s.r.violations = append(s.r.violations, fmt.Sprintf("%s retired stale reserve %s before its replacement was ready",
			s.now.Format(time.TimeOnly), h.InstanceType))
	}
}
