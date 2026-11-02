package control

import (
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// Each version that becomes active, by deploy or by start on a version,
// announces workload.deployed with the version it replaced; a deploy that
// changes nothing announces nothing.
func TestActivatingAVersionAnnouncesWorkloadDeployed(t *testing.T) {
	t.Parallel()
	pool, ws := fixture(t)
	c := NewControl(pool)
	if _, err := pool.Exec(t.Context(), `insert into webhooks (workspace_id, url, events)
values ($1, 'https://example.com/hook', array['workload.deployed'])`, uuid.UUID(ws)); err != nil {
		t.Fatal(err)
	}
	deploy(t, c, ws, false, function("summarize"))
	deploy(t, c, ws, false, function("summarize"))
	deploy(t, c, ws, false, withConcurrency(function("summarize"), 2))
	id, err := c.FindWorkload(t.Context(), ws, WorkloadRef{App: "reports", Kind: apitypes.WorkloadKindFunction, Name: "summarize"})
	if err != nil {
		t.Fatal(err)
	}
	one := 1
	if _, err := c.StartWorkload(t.Context(), ws, id, &one); err != nil {
		t.Fatal(err)
	}

	rows, err := pool.Query(t.Context(), `select data ->> 'app', data -> 'workload' ->> 'version', coalesce(data ->> 'previous_version', '')
from webhook_deliveries where event = 'workload.deployed' order by id`)
	if err != nil {
		t.Fatal(err)
	}
	var got [][3]string
	for rows.Next() {
		var d [3]string
		if err := rows.Scan(&d[0], &d[1], &d[2]); err != nil {
			t.Fatal(err)
		}
		got = append(got, d)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	want := [][3]string{{"reports", "1", ""}, {"reports", "2", "1"}, {"reports", "1", "2"}}
	if len(got) != len(want) {
		t.Fatalf("announced %v, want %v", got, want)
	}
	for n := range want {
		if got[n] != want[n] {
			t.Fatalf("announced %v, want %v", got, want)
		}
	}
}
