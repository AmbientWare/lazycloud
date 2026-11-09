package compute_test

import (
	"math"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// The fleet's spend counts the platform's own cloud hosts: a running one at
// its hourly price for the time it ran, a stopped one at its volumes for the
// time it lay stopped, including a reserve whose stop is older than the
// phase history keeps. Hosts in a customer's account and joined machines
// cost the platform nothing.
func TestFleetSpendCountsThePlatformsOwnHostsOverTheirHistory(t *testing.T) {
	t.Parallel()
	o, _, _ := reserveFleet(t)
	running := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Market: compute.MarketOnDemand,
		Region: "us-east-2", Zone: "us-east-2a", InstanceID: "i-0000000000000f001"})
	run(t, o.pool, `update hosts set instance_type = 'm7i.large', hourly_micros = 3600000 where id = $1`, uuid.UUID(running))
	run(t, o.pool, `update host_phase_changes set at = now() - interval '2 hours' where host_id = $1`, uuid.UUID(running))

	stopped := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Phase: compute.PhaseStopped, Market: compute.MarketSpot,
		Region: "us-east-2", Zone: "us-east-2a", InstanceID: "i-0000000000000f002"})
	run(t, o.pool, `update hosts set instance_type = 'c6a.2xlarge', hourly_micros = 900000 where id = $1`, uuid.UUID(stopped))
	run(t, o.pool, `update host_phase_changes set at = now() - interval '40 days' where host_id = $1`, uuid.UUID(stopped))
	run(t, o.pool, `insert into host_phase_changes (host_id, at, phase, capacity_state)
values ($1, now() - interval '35 days', 'stopped', 'draining')`, uuid.UUID(stopped))

	for _, kind := range []compute.HostKind{compute.KindConnection, compute.KindMachine} {
		other := newHost(t, o.pool, hostSpec{Kind: kind, Provider: compute.ProviderAWS, Market: compute.MarketOnDemand,
			Region: "us-east-2", Zone: "us-east-2a", InstanceID: "i-other-" + string(kind)})
		run(t, o.pool, `update hosts set instance_type = 'm7i.large', hourly_micros = 50000000 where id = $1`, uuid.UUID(other))
	}

	if err := o.compute.Reconcile(t.Context(), discard()); err != nil {
		t.Fatal(err)
	}
	spend, err := o.compute.FleetSpend(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if spend.Running != (compute.SpendShare{Hosts: 1, HourlyMicros: 3_600_000}) {
		t.Fatalf("running %+v, want the one platform host at $3.60 an hour", spend.Running)
	}
	if spend.Stopped.Hosts != 1 || spend.Stopped.HourlyMicros <= 0 || spend.Stopped.HourlyMicros >= 900_000 {
		t.Fatalf("stopped %+v, want the reserve at its volumes, less than its running price", spend.Stopped)
	}
	want := 2*3_600_000 + 24*spend.Stopped.HourlyMicros
	if math.Abs(float64(spend.WindowMicros-want)) > 2_000 {
		t.Fatalf("last 24 hours cost %d micros, want about %d: 2 h running and 24 h stopped", spend.WindowMicros, want)
	}
}
