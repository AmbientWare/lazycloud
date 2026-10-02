package compute

import (
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// The admin fleet reads touch about as many buffers with 20,000 deleted
// platform hosts as with 1,000: they read live hosts, not host history.
func TestFleetAdminReadsStayFlatAsDeletedHostsGrow(t *testing.T) {
	pool := dbtest.New(t)
	insert := func(n int, phase string) {
		costExec(t, pool, `
insert into hosts (name, state, cpu_millis, memory_bytes, provider, kind, phase, instance_id)
select 'lazycloud-m7i.xlarge', 'offline', 4000, 16::bigint << 30, 'aws', 'platform', $1, 'i-' || gen_random_uuid()
from generate_series(1, $2)`, phase, n)
	}
	insert(20, "ready")
	measure := func() map[string]int {
		costExec(t, pool, "analyze hosts")
		out := map[string]int{}
		for _, mode := range []string{"force_custom_plan", "force_generic_plan"} {
			out["rollout"] = max(out["rollout"], planBuffers(explainPlan(t, pool, mode, fleetRollout, time.Now(), "1.0.0")))
			out["nodes"] = max(out["nodes"], planBuffers(explainPlan(t, pool, mode, platformHosts, uuid.Nil, 51)))
		}
		return out
	}
	insert(1000, "deleted")
	small := measure()
	insert(19000, "deleted")
	large := measure()
	for name, before := range small {
		t.Logf("%s: %d buffers with 1,000 deleted hosts, %d with 20,000", name, before, large[name])
		if large[name] > before+10 {
			t.Errorf("%s reads %d buffers with 20,000 deleted hosts, %d with 1,000", name, large[name], before)
		}
	}
}
