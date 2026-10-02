package compute

import (
	"io"
	"log/slog"
	"slices"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// BenchmarkPlanCapacity measures one capacity pass over 2,000 pending
// containers from 20 workspaces that fit no host, packed onto new platform
// hosts in two regions. Each iteration starts with nothing bought. Run with
// -bench PlanCapacity -benchtime 20x.
func BenchmarkPlanCapacity(b *testing.B) {
	pool := dbtest.New(b)
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	network := func(zone string) Network {
		return Network{VPCID: "vpc-1", SecurityGroupID: "sg-1", Subnets: []Subnet{{ID: "subnet-" + zone, Zone: zone}}}
	}
	c := NewCompute(pool, nil, Config{Fleet: Fleet{
		MaxHosts: 1000, Networks: map[string]Network{"us-east-2": network("us-east-2a"), "us-west-1": network("us-west-1a")},
	}})
	exec(b, pool, `
with ws as (insert into workspaces (name) select 'ws-' || n from generate_series(1, 20) n returning id),
     app as (insert into apps (workspace_id, name, state) select id, 'app', 'active' from ws returning id, workspace_id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id, app_id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select wl.id, 1, '{"resources": {"cpu_millis": 1000, "memory_mib": 2048}}', sha256('spec'), sha256('src')
             from wl returning id, workload_id)
insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes)
select app.workspace_id, rel.id, 'pending', 1, 1000, 2::bigint << 30
from rel join wl on wl.id = rel.workload_id join app on app.id = wl.app_id, generate_series(1, 100)`)
	exec(b, pool, "analyze")

	var times []time.Duration
	var requested int
	for b.Loop() {
		b.StopTimer()
		exec(b, pool, "update containers set capacity_wait = null")
		exec(b, pool, "delete from hosts")
		b.StartTimer()
		start := time.Now()
		result, err := c.PlanCapacity(b.Context(), logger)
		if err != nil {
			b.Fatal(err)
		}
		times = append(times, time.Since(start))
		requested = result.Requested
	}
	slices.Sort(times)
	b.ReportMetric(float64(times[len(times)/2].Microseconds())/1000, "p50-ms")
	b.ReportMetric(float64(times[len(times)*95/100].Microseconds())/1000, "p95-ms")
	b.ReportMetric(float64(requested), "hosts")
}
