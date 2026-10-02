package compute

import (
	"testing"
	"time"
)

const mib = int64(1) << 20

func shape(cpu, memMiB int64, gpus int) FleetCapacity {
	return FleetCapacity{CPUMillis: cpu, MemoryBytes: memMiB * mib, GPUs: gpus}
}

func TestForecastUsesOnlyRecentArrivalsAndKeepsPending(t *testing.T) {
	pending := shape(2000, 4096, 1)
	excluded := shape(100_000, 1_000_000, 100)
	f := ForecastDemand(DefaultPolicy(), offerNow, []Arrival{
		{At: offerNow.Add(time.Microsecond), Shape: excluded, Count: 1},
		{At: offerNow.Add(-600 * time.Second), Shape: excluded, Count: 1},
	}, nil, pending, nil, 10*time.Second, 180*time.Second)
	if f.Warm != pending || f.Total != pending || f.Samples != 0 || len(f.Shapes) != 0 {
		t.Fatalf("forecast %+v", f)
	}
}

func TestForecastHorizonsCountPendingAndTheBurstOnce(t *testing.T) {
	request, pending := shape(1000, 2048, 1), shape(500, 1024, 0)
	f := ForecastDemand(DefaultPolicy(), offerNow, []Arrival{{At: offerNow.Add(-time.Second), Shape: request, Count: 2}},
		nil, pending, nil, 60*time.Second, 120*time.Second)
	if f.Warm != pending.Plus(request.Times(3)) || f.Total != pending.Plus(request.Times(5)) || f.Burst != request || f.Samples != 2 {
		t.Fatalf("forecast %+v", f)
	}
}

func TestForecastBoundsShapesWithoutHidingOneAndRoundsUp(t *testing.T) {
	var arrivals []Arrival
	for i := int64(1); i <= 64; i++ {
		arrivals = append(arrivals, Arrival{At: offerNow, Shape: FleetCapacity{CPUMillis: i, MemoryBytes: 100 - i, GPUs: 1}, Count: 1})
	}
	f := ForecastDemand(DefaultPolicy(), offerNow, arrivals, nil, FleetCapacity{}, nil, 10*time.Millisecond, 10*time.Millisecond)
	if len(f.Shapes) > 32 {
		t.Fatalf("%d shapes", len(f.Shapes))
	}
	for _, a := range arrivals {
		if !everyShapeFits([]FleetCapacity{a.Shape}, f.Shapes) {
			t.Fatalf("shape %+v hidden", a.Shape)
		}
	}
	if want := (FleetCapacity{CPUMillis: 65, MemoryBytes: 100, GPUs: 2}); f.Warm != want || f.Total != want {
		t.Fatalf("warm %+v total %+v", f.Warm, f.Total)
	}
}

func TestScheduledDemandUsesItsHorizonWithoutBecomingAnArrivalRate(t *testing.T) {
	request := shape(2000, 4096, 0)
	f := ForecastDemand(DefaultPolicy(), offerNow, nil, []Arrival{
		{At: offerNow.Add(10 * time.Second), Shape: request, Count: 2},
		{At: offerNow.Add(60 * time.Second), Shape: request, Count: 1},
		{At: offerNow.Add(61 * time.Second), Shape: request.Times(100), Count: 1},
	}, FleetCapacity{}, nil, 10*time.Second, 60*time.Second)
	if f.Warm != request.Times(2) || f.Total != request.Times(3) || f.Samples != 0 || !f.Burst.Empty() ||
		!f.ShortArrivals.Empty() || f.ScheduledTotal != request.Times(3) || f.Largest != request {
		t.Fatalf("forecast %+v", f)
	}
}

func TestShortJobsAndSeparateSchedulesUseOccupancy(t *testing.T) {
	request := shape(1000, 2048, 0)
	var scheduled []Arrival
	for _, offset := range []time.Duration{10, 30, 50} {
		scheduled = append(scheduled, Arrival{At: offerNow.Add(offset * time.Second), Shape: request, Count: 1, Duration: 10 * time.Second})
	}
	f := ForecastDemand(DefaultPolicy(), offerNow, []Arrival{{At: offerNow, Shape: request, Count: 60, Duration: 5 * time.Second}},
		scheduled, FleetCapacity{}, nil, 60*time.Second, 300*time.Second)
	if f.ScheduledTotal != request || f.Total != request.Times(7) || f.Warm != f.Total {
		t.Fatalf("forecast %+v", f)
	}
}

func TestScheduledRunsShareConcurrencyAndWarmContainers(t *testing.T) {
	request := shape(1000, 1024, 0)
	w := ScheduledWorkload{Shape: request, Concurrency: 2, MaxContainers: 2, KeepWarm: 60 * time.Second, Duration: 30 * time.Second}
	for _, at := range []time.Duration{30, 40, 50, 60, 180} {
		w.Runs = append(w.Runs, ScheduledRun{At: offerNow.Add(at * time.Second), Count: 1})
	}
	until := offerNow.Add(300 * time.Second)
	arrivals := ScheduledArrivals([]ScheduledWorkload{w}, offerNow, until)
	if len(arrivals) != 3 {
		t.Fatalf("%d containers: %+v", len(arrivals), arrivals)
	}
	f := ForecastDemand(DefaultPolicy(), offerNow, nil, arrivals, FleetCapacity{}, nil, 120*time.Second, 300*time.Second)
	if f.ScheduledWarm != request.Times(2) || f.ScheduledTotal != request.Times(2) {
		t.Fatalf("scheduled warm %+v total %+v", f.ScheduledWarm, f.ScheduledTotal)
	}
	w.AlwaysWarm, w.Existing = true, 2
	if arrivals := ScheduledArrivals([]ScheduledWorkload{w}, offerNow, until); len(arrivals) != 0 {
		t.Fatalf("kept-warm containers serve the runs: %+v", arrivals)
	}
}

func activationHost(state FleetState) FleetHost {
	return FleetHost{
		ID: HostID{1}, InstanceType: "c6a.2xlarge", Region: "us-east-1", Market: MarketOnDemand,
		Usable: shape(8000, 16_384, 0), State: state, Current: true,
	}
}

func restored() ActivationStat {
	return ActivationStat{
		Kind: ActivationResume, InstanceType: "c6a.2xlarge", Region: "us-east-1",
		Outcome: ptr(ResumeMemoryRestored), Ready: 30, P95: ptr(10 * time.Second),
	}
}

func TestFragmentedReservesCannotShortenALargeRequestsForecast(t *testing.T) {
	var hosts []FleetHost
	for i := range 8 {
		h := activationHost(FleetImageSaved)
		h.ID = HostID{byte(i + 1)}
		hosts = append(hosts, h)
	}
	request := shape(16_000, 32_768, 0)
	f := ForecastMarket(DefaultPolicy(), offerNow, ForecastInput{
		Hosts: hosts, Stats: []ActivationStat{restored()}, Pending: request, PendingShapes: []FleetCapacity{request},
	})
	if f.WarmHorizon != 360*time.Second || f.Timing == nil || f.Timing.Kind != ActivationProvision {
		t.Fatalf("forecast horizon %s timing %+v", f.WarmHorizon, f.Timing)
	}
}

func TestResumeEstimatesTakeColdBootsFromMatchingHardware(t *testing.T) {
	host := activationHost(FleetImageSaved)
	cold := restored()
	cold.Outcome, cold.Ready, cold.P95 = ptr(ResumeColdBoot), 1, ptr(145*time.Second)
	in := ForecastInput{Hosts: []FleetHost{host}, Stats: []ActivationStat{restored(), cold}, Pending: host.Usable, PendingShapes: []FleetCapacity{host.Usable}}
	f := ForecastMarket(DefaultPolicy(), offerNow, in)
	if f.WarmHorizon != 205*time.Second || f.Timing.ColdBoots != 1 {
		t.Fatalf("horizon %s timing %+v", f.WarmHorizon, f.Timing)
	}
	cold.InstanceType = "m6a.8xlarge"
	in.Stats = []ActivationStat{restored(), cold}
	if f := ForecastMarket(DefaultPolicy(), offerNow, in); f.WarmHorizon != 70*time.Second {
		t.Fatalf("other hardware's cold boot counted: %s", f.WarmHorizon)
	}
}

func TestHeldPreparationsDoNotSetServingTiming(t *testing.T) {
	host := activationHost(FleetImageSaved)
	held := restored()
	held.Ready, held.P95 = 0, nil
	f := ForecastMarket(DefaultPolicy(), offerNow, ForecastInput{Hosts: []FleetHost{host}, Stats: []ActivationStat{held}, Pending: host.Usable})
	if f.WarmHorizon != 90*time.Second || !f.Timing.Fallback {
		t.Fatalf("horizon %s timing %+v", f.WarmHorizon, f.Timing)
	}
}

func TestActivationFailuresFallBackToProvisionAndPlainStopsBoot(t *testing.T) {
	host := activationHost(FleetStopped)
	in := ForecastInput{Hosts: []FleetHost{host}, Pending: host.Usable}
	if f := ForecastMarket(DefaultPolicy(), offerNow, in); f.WarmHorizon != 180*time.Second || f.Timing.Kind != ActivationBoot {
		t.Fatalf("plain stop: %s %+v", f.WarmHorizon, f.Timing)
	}
	in.Stats = []ActivationStat{{Kind: ActivationBoot, InstanceType: host.InstanceType, Region: host.Region, Ready: 30, Failed: 1, P95: ptr(40 * time.Second)}}
	if f := ForecastMarket(DefaultPolicy(), offerNow, in); f.WarmHorizon != 360*time.Second {
		t.Fatalf("failed boots: %s", f.WarmHorizon)
	}
}
