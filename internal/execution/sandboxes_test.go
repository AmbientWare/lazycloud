package execution

import (
	"slices"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// Search finds sandboxes by part of their name, their app's name or their
// container id, ignoring case, so the dashboard's global search reaches them.
func TestSandboxSearchMatchesNameAppAndContainerID(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{}`)
	var coder, scratch string
	err := pool.QueryRow(t.Context(), `
with app as (select id, workspace_id from apps where name = 'reports'),
     wl as (insert into workloads (app_id, kind, name, desired_state)
            select id, 'sandbox', n, 'active' from app, unnest(array['Coder', 'scratch']) n returning id, name),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, jsonb_build_object('kind', 'sandbox', 'name', name), sha256(name::bytea), sha256('src') from wl returning id, workload_id),
     ctr as (insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes)
             select app.workspace_id, rel.id, 'pending', 1, 1000, 1 << 28 from app, rel returning id, release_id)
select (select ctr.id::text from ctr join rel on rel.id = ctr.release_id join wl on wl.id = rel.workload_id where wl.name = 'Coder'),
       (select ctr.id::text from ctr join rel on rel.id = ctr.release_id join wl on wl.id = rel.workload_id where wl.name = 'scratch')`).Scan(&coder, &scratch)
	if err != nil {
		t.Fatal(err)
	}
	found := func(term string) []string {
		t.Helper()
		page, err := e.ListSandboxes(t.Context(), f.workspace, SandboxFilter{Search: &term}, 10, "")
		if err != nil {
			t.Fatal(err)
		}
		var ids []string
		for _, s := range page.Sandboxes {
			ids = append(ids, s.Id.String())
		}
		slices.Sort(ids)
		return ids
	}
	if got := found("cOdE"); !slices.Equal(got, []string{coder}) {
		t.Fatalf("by name %v, want %s", got, coder)
	}
	if got := found("report"); len(got) != 2 {
		t.Fatalf("by app %v, want both", got)
	}
	if got := found(scratch[24:]); !slices.Equal(got, []string{scratch}) {
		t.Fatalf("by container id %v, want %s", got, scratch)
	}
	if got := found("nothing"); len(got) != 0 {
		t.Fatalf("no match %v", got)
	}
}
