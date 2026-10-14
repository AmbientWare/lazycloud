package edge

import (
	"encoding/json"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// An app's request listing pages newest first through its own requests, and
// reads none of the workspace's other apps' requests however many there are.
func TestRequestListingReadsOnlyTheAppsRequests(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	e := newTestEdge(t, pool)
	ws, wl, rel, _ := releaseFixture(t, pool)
	var other, otherRelease uuid.UUID
	if err := pool.QueryRow(t.Context(), `
with a as (insert into apps (workspace_id, name, state) values ($1, 'busy', 'active') returning id),
     w as (insert into workloads (app_id, kind, name, desired_state) select id, 'endpoint', 'api', 'active' from a returning id),
     r as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
           select id, 1, '{}'::jsonb, sha256('spec'), sha256('src') from w returning id)
select w.id, r.id from w, r`, ws).Scan(&other, &otherRelease); err != nil {
		t.Fatal(err)
	}
	insert := `insert into http_requests (id, workspace_id, workload_id, release_id, method, path, status, started_at, duration_ms, request_bytes, response_bytes)
select uuidv7(), $1, $2, $3, 'GET', '/', 200, now(), 1, 0, 0 from generate_series(1, $4)`
	exec := func(workload, release uuid.UUID, n int) {
		if _, err := pool.Exec(t.Context(), insert, ws, workload, release, n); err != nil {
			t.Fatal(err)
		}
	}
	exec(wl, rel, 3)
	exec(other, otherRelease, 5_000)
	// Other workspaces' workloads, as the table holds in production.
	if _, err := pool.Exec(t.Context(), `
with ws as (insert into workspaces (name) select 'ws' || n from generate_series(1, 30) n returning id),
     a as (insert into apps (workspace_id, name, state) select id, 'app', 'active' from ws returning id, workspace_id),
     w as (insert into workloads (app_id, kind, name, desired_state) select id, 'endpoint', 'web', 'active' from a returning id, app_id),
     r as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
           select id, 1, '{}'::jsonb, sha256('spec'), sha256('src') from w returning id, workload_id)
insert into http_requests (id, workspace_id, workload_id, release_id, method, path, status, started_at, duration_ms, request_bytes, response_bytes)
select uuidv7(), a.workspace_id, w.id, r.id, 'GET', '/', 200, now(), 1, 0, 0
from r join w on w.id = r.workload_id join a on a.id = w.app_id, generate_series(1, 20)`); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(t.Context(), "analyze http_requests"); err != nil {
		t.Fatal(err)
	}

	first, next, err := e.ListRequests(t.Context(), identity.WorkspaceID(ws), "shop", nil, nil, 2)
	if err != nil || len(first) != 2 || next == nil || first[0].Id.String() <= first[1].Id.String() {
		t.Fatalf("first page %+v next %v: %v; want the 2 newest of shop's 3", first, next, err)
	}
	rest, after, err := e.ListRequests(t.Context(), identity.WorkspaceID(ws), "shop", nil, next, 2)
	if err != nil || len(rest) != 1 || after != nil || rest[0].Id.String() >= first[1].Id.String() {
		t.Fatalf("second page %+v next %v: %v; want shop's oldest and no cursor", rest, after, err)
	}

	var plan []struct{ Plan planNode }
	var raw []byte
	if err := pool.QueryRow(t.Context(), "explain (analyze, format json) "+listRequests,
		ws, "shop", nil, nil, int32(51)).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &plan); err != nil {
		t.Fatal(err)
	}
	if read := plan[0].Plan.rowsOn("http_requests"); read > 3 {
		t.Fatalf("listing shop's 3 requests read %d request rows", read)
	}
}

// planNode is one node of a JSON query plan.
type planNode struct {
	Relation string     `json:"Relation Name"`
	Rows     float64    `json:"Actual Rows"`
	Loops    float64    `json:"Actual Loops"`
	Removed  float64    `json:"Rows Removed by Filter"`
	Plans    []planNode `json:"Plans"`
}

// rowsOn is how many rows the plan's scans of relation returned or filtered
// out.
func (n planNode) rowsOn(relation string) int {
	read := 0
	if n.Relation == relation {
		read = int(n.Rows*n.Loops + n.Removed)
	}
	for _, child := range n.Plans {
		read += child.rowsOn(relation)
	}
	return read
}
