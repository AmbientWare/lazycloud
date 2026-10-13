package compute

import (
	"fmt"
	"slices"
	"testing"
	"time"
)

// Scenarios of GPU Spot capacity: pools that refuse, Spot requests slow to
// disable, and releases that change the agent.

// l4Job is the Spot-tolerant L4 function prod ran on 2026-10-13.
var l4Job = Requirement{Preemptible: true, GPUs: []string{"L4"}, GPUCount: 1, CPUMillis: 2000, MemoryBytes: 13056 << 20}

// gpuSpot reports a Spot pool of a GPU type.
func gpuSpot(typ string, market Market) bool {
	c, _ := CatalogTypeNamed(typ)
	return market == MarketSpot && c.GPU != ""
}

// oversized names an offer bought or tried for an L4 job that has more
// than its one card or exceeds the size cap.
func oversized(p Policy, o FleetOffer) string {
	switch {
	case o.Type.GPUCount != 1:
		return fmt.Sprintf("%s has %d cards", o.Key(), o.Type.GPUCount)
	case !p.LargestShape.Cap.Covers(o.Usable):
		return fmt.Sprintf("%s exceeds the size cap", o.Key())
	}
	return ""
}

// Work purchases follow the size rule: with every L4 Spot pool refusing,
// as on 2026-10-13, the 2 CPU / 12.75 GiB job's purchases climb the
// single-card sizes no further than the size cap, never to a g6.8xlarge,
// a 4-card g6.12xlarge or an 8-card g6.48xlarge. Its margin keeps it off
// on-demand, so it starts once Spot fills again.
func TestAnL4JobBuysNoLargerHostThanItNeeds(t *testing.T) {
	t.Parallel()
	s := newChurnSim(t)
	s.tick = simLongTick
	refill := s.start.Add(time.Hour)
	s.refuses = func(_, _, typ string, market Market) bool { return gpuSpot(typ, market) && s.now.Before(refill) }
	s.run(30*time.Minute, nil)
	s.start, s.until, s.r = s.now, time.Time{}, simResult{}
	r := s.run(90*time.Minute, []simArrival{{need: l4Job, count: 1, runs: 10 * time.Minute, fn: "gpu"}})
	for _, p := range r.bought {
		for _, o := range p.tried {
			if why := oversized(s.p, o); why != "" && p.containers > 0 {
				t.Errorf("%s: %s", p.at.Format(time.TimeOnly), why)
			}
		}
	}
	if len(r.fnWaits["gpu"]) != 1 || len(r.violations) > 0 {
		t.Errorf("placed %d of 1, violations %v", len(r.fnWaits["gpu"]), r.violations)
	}
}

// The size rule steps up while smaller pools refuse: with every g6.xlarge
// Spot pool refusing, the L4 job lands on a g6.2xlarge within one
// cooldown.
func TestAnL4JobStepsUpWhileTheSmallestSizeRefuses(t *testing.T) {
	t.Parallel()
	s := newChurnSim(t)
	s.tick = simLongTick
	s.refuses = func(_, _, typ string, market Market) bool { return typ == "g6.xlarge" && market == MarketSpot }
	s.run(30*time.Minute, nil)
	s.start, s.until, s.r = s.now, time.Time{}, simResult{}
	// The L4 reserve is busy, so the job buys.
	for _, h := range s.hosts {
		if h.market() == (ReserveMarket{Preemptible: true, GPU: "L4"}) {
			h.Protected = true
		}
	}
	r := s.run(30*time.Minute, []simArrival{{need: l4Job, count: 1, runs: 10 * time.Minute, fn: "gpu"}})
	waits := r.fnWaits["gpu"]
	if len(waits) != 1 || waits[0] > simCooldown || len(r.violations) > 0 {
		t.Fatalf("waits %v, violations %v; want the job placed within a cooldown", waits, r.violations)
	}
	i := slices.IndexFunc(r.bought, func(p simPurchase) bool { return p.containers > 0 && p.launched })
	if i < 0 || r.bought[i].tried[len(r.bought[i].tried)-1].Type.Name != "g6.2xlarge" {
		t.Errorf("purchases %+v, want the job on a g6.2xlarge", r.bought)
	}
}

// Every pool one launch walk refuses cools until one end, however many
// passes the walk takes, so its market buys again once rather than as each
// pool comes back. Over two hours of refused GPU Spot reserves, each
// market's purchases start at least a cooldown apart after a refusal.
func TestAWalksRefusedPoolsShareOneEnd(t *testing.T) {
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

// A deploy that changes the agent while every GPU Spot pool refuses leaves
// each GPU market its stale reserve: no replacement can be bought, so the
// stale one stays, and the next L4 job resumes it, updates its agent and
// runs there sooner than a purchase could serve it.
func TestAnAgentChangeKeepsStaleReservesServing(t *testing.T) {
	t.Parallel()
	s := newChurnSim(t)
	s.tick = simLongTick
	s.run(time.Hour, nil)
	s.start, s.until, s.r = s.now, time.Time{}, simResult{}
	s.refuses = func(_, _, typ string, market Market) bool { return gpuSpot(typ, market) }
	s.startsFill = true
	s.deploys = []simDeploy{{at: time.Minute, agent: true}}
	r := s.run(30*time.Minute, []simArrival{{at: 10 * time.Minute, need: l4Job, count: 1, runs: 5 * time.Minute, fn: "gpu"}})
	if r.starts[startResumed] != 1 || len(r.violations) > 0 {
		t.Errorf("the L4 job after the deploy: starts %v, violations %v", r.starts, r.violations)
	}
	if waits := r.fnWaits["gpu"]; len(waits) != 1 || waits[0] > s.p.Starts.GPULaunch {
		t.Errorf("the L4 job waited %v, want less than a purchase's %s", waits, s.p.Starts.GPULaunch)
	}
}

// EC2 holds a stopped Spot reserve's request active for up to 165 s. L4
// work that waits for the reserve keeps it: nothing retires the reserve,
// and the work starts once EC2 disables the request and the reserve
// resumes. A request active past the planner's bound buys the work a host
// instead, still retiring nothing.
func TestASlowSpotDisableKeepsTheReserve(t *testing.T) {
	t.Parallel()
	st := DefaultPolicy().Starts
	stopped := st.GPULaunch + simPrepare + simGPUStop
	for _, gap := range []time.Duration{146 * time.Second, 165 * time.Second, 10 * time.Minute} {
		s := newSim(t, DefaultPolicy())
		s.disableGap = gap
		at := stopped + 33*time.Second
		r := s.run(at+20*time.Minute, []simArrival{{at: at, need: l4Job, count: 1, runs: time.Minute, fn: "gpu"}})
		if len(r.retired) > 0 || len(r.violations) > 0 {
			t.Errorf("request active %s: reserves retired at %v, violations %v", gap, r.retired, r.violations)
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

// A reserve resumed for work is awaited however long ago it stopped: the
// L4 job resumes the Spot reserve stopped an hour before and buys nothing.
func TestAResumedReserveIsAwaitedNotBoughtAgain(t *testing.T) {
	t.Parallel()
	s := newSim(t, DefaultPolicy())
	s.tick = simLongTick
	s.run(time.Hour, nil)
	s.start, s.until, s.r, s.tick = s.now, time.Time{}, simResult{}, simTick
	r := s.run(10*time.Minute, []simArrival{{need: l4Job, count: 1, runs: 5 * time.Minute, fn: "gpu"}})
	bought := slices.ContainsFunc(r.bought, func(p simPurchase) bool { return p.containers > 0 })
	if r.starts[startResumed] != 1 || bought || len(r.violations) > 0 {
		t.Errorf("starts %v, bought for the job %t, violations %v; want one resume and no purchase", r.starts, bought, r.violations)
	}
}
