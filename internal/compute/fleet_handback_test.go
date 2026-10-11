package compute

import (
	"math"
	"math/rand/v2"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/cpu"
)

// simFunctions runs functions' tasks in reused containers as execution
// plans them: a container per queued or running task up to the maximum,
// idle ones past keep-warm draining. A container whose host hands
// Spot-tolerant work back is replaced first: a busy one drains at once and
// finishes its task, an idle one claims until its replacement is ready,
// and one whose replacement is ready drains.
type simFunctions struct {
	fns []*simFn
	// waits are tasks' start waits; created counts containers.
	waits   []time.Duration
	created int
	// after is when the measures below start: on-demand host seconds
	// while every container on the host could move to Spot, and task
	// seconds on Spot and on on-demand hosts.
	after                        time.Time
	movableOnDemandSec           float64
	spotTaskSec, onDemandTaskSec float64
	movableOnDemandMicros        int64
}

type simFn struct {
	name     string
	need     Requirement
	task     time.Duration
	every    time.Duration
	from, to time.Duration
	keepWarm time.Duration
	max      int

	arrivals   *rand.Rand
	queue      []time.Time
	containers []*simContainer
}

type execContainer struct {
	// busy is set while it runs a task, until busyUntil.
	busy      bool
	busyUntil time.Time
	lastEnd   time.Time
	draining  bool
	// replaces and replacedBy pair a leaving container with its
	// replacement; replacedAt is when the replacement was ready, where the
	// replaced container stops billing.
	replaces, replacedBy *simContainer
	replacedAt           time.Time
}

func (f *simFn) ready(c *simContainer, now time.Time) bool {
	return c.on != nil && !now.Before(c.started.Add(containerStart))
}

// leaving reports a container that leaves its host: its replacement is
// ready, or it can move and its host hands Spot-tolerant work back.
func (f *simFn) leaving(c *simContainer) bool {
	return !c.exec.replacedAt.IsZero() || c.on != nil && c.on.HandsBack && c.need.Preemptible && c.need.Disks == 0
}

func (e *simFunctions) step(s *sim) {
	now, secs := s.now, s.tick.Seconds()
	for _, f := range e.fns {
		if at := now.Sub(s.start); at >= f.from && at < f.to {
			// Poisson arrivals, one per every on average.
			k, p, l := 0, f.arrivals.Float64(), math.Exp(-secs/f.every.Seconds())
			for ; p > l; p *= f.arrivals.Float64() {
				k++
			}
			for range k {
				f.queue = append(f.queue, now)
			}
		}
		live := f.containers[:0]
		for _, c := range f.containers {
			x := c.exec
			if x.busy && !x.busyUntil.After(now) {
				x.busy, x.lastEnd = false, now
			}
			if old := x.replaces; old != nil && f.ready(c, now) {
				if old.exec.replacedAt.IsZero() && old.ends.After(now) {
					old.exec.replacedAt = c.started.Add(containerStart)
				}
				x.replaces = nil
			}
			if x.draining && !x.busy {
				if c.on != nil {
					c.ends = now
				} else {
					s.pending = slices.DeleteFunc(s.pending, func(p *simContainer) bool { return p == c })
					if old := x.replaces; old != nil {
						old.exec.replacedBy = nil
					}
				}
				continue
			}
			live = append(live, c)
		}
		f.containers = live
		e.plan(s, f)
		for _, c := range f.containers {
			if len(f.queue) == 0 {
				break
			}
			if x := c.exec; f.ready(c, now) && !x.draining && !x.busy {
				x.busy, x.busyUntil = true, now.Add(f.task)
				e.waits, f.queue = append(e.waits, now.Sub(f.queue[0])), f.queue[1:]
			}
		}
	}
	if now.Before(e.after) {
		return
	}
	for _, h := range s.hosts {
		if h.reserve() || len(h.containers) == 0 {
			continue
		}
		movable := true
		for _, c := range h.containers {
			movable = movable && c.exec != nil && c.need.Preemptible && c.need.Disks == 0
			if c.exec != nil && c.exec.busy && c.need.Preemptible {
				if h.Market == MarketSpot {
					e.spotTaskSec += secs
				} else {
					e.onDemandTaskSec += secs
				}
			}
		}
		if movable && h.Market == MarketOnDemand {
			e.movableOnDemandSec += secs
			e.movableOnDemandMicros += h.offer.HourlyMicros * int64(secs)
		}
	}
}

// plan is execution's planning for f.
func (e *simFunctions) plan(s *sim, f *simFn) {
	now := s.now
	running := 0
	for _, c := range f.containers {
		if c.exec.busy {
			running++
		}
	}
	desired := min(len(f.queue)+running, f.max)
	var leaving, unpaired, idle []*simContainer
	for _, c := range f.containers {
		x := c.exec
		if !f.leaving(c) {
			continue
		}
		if !x.draining {
			leaving = append(leaving, c)
			x.draining = !x.replacedAt.IsZero() || x.busy
		}
		if x.replacedBy == nil && x.replacedAt.IsZero() {
			unpaired = append(unpaired, c)
			if !x.draining {
				idle = append(idle, c)
			}
		}
	}
	live, active := 0, 0
	for _, c := range f.containers {
		live++
		if !c.exec.draining {
			active++
		}
	}
	for _, c := range leaving {
		if !c.exec.draining {
			active--
		}
	}
	drain := func(cs []*simContainer) {
		for _, c := range cs {
			c.exec.draining = true
		}
	}
	if active < desired {
		count := min(desired-active, f.max-live)
		if count <= 0 {
			drain(idle[:min(desired-active, len(idle))])
			return
		}
		for n := range count {
			c := &simContainer{id: uuid.New(), need: f.need, arrived: now, runs: 1000 * time.Hour, fn: f.name, exec: &execContainer{}}
			if n < len(unpaired) {
				c.exec.replaces, unpaired[n].exec.replacedBy = unpaired[n], c
			}
			f.containers = append(f.containers, c)
			s.pending = append(s.pending, c)
			e.created++
		}
		return
	}
	drain(idle)
	excess := active - desired
	for _, c := range f.containers {
		if excess > 0 && c.on == nil && !c.exec.draining {
			c.exec.draining, excess = true, excess-1
		}
	}
	idleStay := slices.DeleteFunc(slices.Clone(f.containers), func(c *simContainer) bool {
		x := c.exec
		return !f.ready(c, now) || x.draining || x.busy || f.leaving(c) || now.Sub(maxTime(x.lastEnd, c.started)) < f.keepWarm
	})
	slices.SortStableFunc(idleStay, func(a, b *simContainer) int {
		return maxTime(a.exec.lastEnd, a.started).Compare(maxTime(b.exec.lastEnd, b.started))
	})
	drain(idleStay[:min(max(excess, 0), len(idleStay))])
}

// handBackScenario is a load of functions and long-lived containers during
// which Spot refuses for a while and recovers.
type handBackScenario struct {
	name     string
	fns      func() []*simFn
	pods     []simArrival
	refuses  func(at time.Duration, gpu bool) bool
	recent   map[ReserveMarket]FleetCapacity
	recovers time.Duration
	// kept is set where work that cannot move keeps on-demand hosts up,
	// whose spare room Spot-tolerant work fills.
	kept bool
}

func taskFn(name string, need Requirement, task, every time.Duration) *simFn {
	return &simFn{name: name, need: need, task: task, every: every, from: 30 * time.Minute, to: 210 * time.Minute, keepWarm: time.Minute, max: 1000}
}

func tolerant(millis cpu.Millis, memGiB int64) Requirement {
	need := cpuNeed(millis, memGiB)
	need.Preemptible = true
	return need
}

func handBackScenarios() []handBackScenario {
	pinned := func() *simFn { return taskFn("pinned", cpuNeed(1000, 2), 30*time.Second, 10*time.Second) }
	refuses := func(from, to time.Duration, gpu bool) func(time.Duration, bool) bool {
		return func(at time.Duration, onGPU bool) bool { return at >= from && at < to && onGPU == gpu }
	}
	cpuRefusal := refuses(25*time.Minute, 60*time.Minute, false)
	return []handBackScenario{
		{name: "short tasks (20s)", recovers: time.Hour, refuses: cpuRefusal, fns: func() []*simFn {
			return []*simFn{taskFn("short", tolerant(1000, 2), 20*time.Second, time.Second), pinned()}
		}},
		{name: "long tasks (20m)", recovers: time.Hour, refuses: cpuRefusal, fns: func() []*simFn {
			return []*simFn{taskFn("long", tolerant(1000, 2), 20*time.Minute, 30*time.Second), pinned()}
		}},
		{name: "pods 3h + short tasks", recovers: time.Hour, refuses: cpuRefusal, kept: true, fns: func() []*simFn {
			return []*simFn{taskFn("short", tolerant(1000, 2), 20*time.Second, 2*time.Second), pinned()}
		}, pods: []simArrival{
			// A burst of on-demand work leaves reserves the pods may borrow.
			{at: 10 * time.Minute, need: cpuNeed(2000, 4), count: 16, runs: 14 * time.Minute, fn: "burst"},
			{at: 32 * time.Minute, need: tolerant(2000, 4), count: 12, runs: 3 * time.Hour, fn: "pod"},
		}},
		{name: "refusals flap every 20m", recovers: 145 * time.Minute, fns: func() []*simFn {
			return []*simFn{taskFn("short", tolerant(1000, 2), 20*time.Second, time.Second), pinned()}
		}, refuses: func(at time.Duration, gpu bool) bool {
			return !gpu && at >= 25*time.Minute && at < 145*time.Minute && (at-25*time.Minute)/(20*time.Minute)%2 == 0
		}},
		{name: "disk function + short tasks", recovers: time.Hour, refuses: cpuRefusal, fns: func() []*simFn {
			disk := taskFn("disk", tolerant(2000, 4), 20*time.Second, 30*time.Second)
			disk.need.Disks, disk.max = 1, 1
			return []*simFn{disk, taskFn("short", tolerant(1000, 2), 20*time.Second, time.Second), pinned()}
		}},
		{name: "T4 tasks (60s)", recovers: time.Hour, refuses: refuses(25*time.Minute, 60*time.Minute, true), fns: func() []*simFn {
			t4 := Requirement{Preemptible: true, GPUs: []string{"T4"}, GPUCount: 1, CPUMillis: 2000, MemoryBytes: 8 * gib}
			return []*simFn{taskFn("t4", t4, time.Minute, 10*time.Second), pinned()}
		}, recent: map[ReserveMarket]FleetCapacity{{GPU: "T4"}: {CPUMillis: 2000, MemoryBytes: 8 * gib, GPUs: 1}}},
	}
}

type handBackResult struct {
	sim                                 simResult
	fns                                 *simFunctions
	revenue, spend                      float64
	waitP50, waitP95                    time.Duration
	movableOnDemandHours, spotTaskShare float64
	movableOnDemandUSD                  float64
	created, leftQueued                 int
}

// runHandBack runs a scenario for four hours and measures from when Spot
// has served again for longer than the longest task and the idle timeout.
func runHandBack(t *testing.T, sc handBackScenario) handBackResult {
	s := newSim(t, DefaultPolicy())
	s.recent = sc.recent
	s.refuses = func(_, _, typ string, market Market) bool {
		ct, _ := CatalogTypeNamed(typ)
		return market == MarketSpot && sc.refuses(s.now.Sub(s.start), ct.GPUCount > 0)
	}
	fns := sc.fns()
	for n, f := range fns {
		f.arrivals = rand.New(rand.NewPCG(uint64(n), 11)) //nolint:gosec // A seeded simulation.
	}
	s.fns = &simFunctions{fns: fns, after: s.start.Add(sc.recovers + simCooldown + 25*time.Minute)}
	r := s.run(4*time.Hour, sc.pods)
	e := s.fns
	out := handBackResult{
		sim: r, fns: e, revenue: float64(r.revenueNanos) / 1e9, spend: float64(r.spendMicros) / 1e6 / 3600,
		waitP50: quantile(e.waits, 0.5), waitP95: quantile(e.waits, 0.95), created: e.created,
		movableOnDemandHours: e.movableOnDemandSec / 3600, movableOnDemandUSD: float64(e.movableOnDemandMicros) / 1e6 / 3600,
		spotTaskShare: e.spotTaskSec / max(e.spotTaskSec+e.onDemandTaskSec, 1),
	}
	for _, f := range fns {
		out.leftQueued += len(f.queue)
	}
	return out
}

// Once Spot serves again, Spot-tolerant work that can move leaves the
// on-demand hosts it borrowed: its tasks run on Spot and no on-demand host
// stays up for it alone, while every task still runs.
func TestSpotTolerantWorkReturnsToSpotOnceItServesAgain(t *testing.T) {
	t.Parallel()
	for _, sc := range handBackScenarios() {
		t.Run(sc.name, func(t *testing.T) {
			t.Parallel()
			r := runHandBack(t, sc)
			t.Logf("%-28s profit $%6.2f revenue $%6.3f spend $%6.3f  task wait p50 %4.1fs p95 %5.1fs  containers %4d  "+
				"later: tasks on Spot %3.0f%%, on-demand hours for movable work %.2f ($%.2f)",
				sc.name, r.revenue-r.spend, r.revenue, r.spend, r.waitP50.Seconds(), r.waitP95.Seconds(), r.created,
				100*r.spotTaskShare, r.movableOnDemandHours, r.movableOnDemandUSD)
			for _, v := range r.sim.violations {
				t.Error(v)
			}
			if r.leftQueued > 0 {
				t.Errorf("%d tasks still queued", r.leftQueued)
			}
			if !sc.kept && r.spotTaskShare < 0.9 || r.movableOnDemandHours > 0.1 {
				t.Errorf("after Spot served again, %.0f%% of Spot-tolerant task time ran on Spot and on-demand hosts ran %.2f hours for work that could move",
					100*r.spotTaskShare, r.movableOnDemandHours)
			}
		})
	}
}

func maxTime(a, b time.Time) time.Time {
	if a.After(b) {
		return a
	}
	return b
}
