package execution

import (
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// Listings read one page through an index whose leading columns match the
// filter, so their cost does not grow with retained history.
func TestListingsReadOnePageOfAnIndex(t *testing.T) {
	pool := dbtest.New(t)
	f := newRelease(t, pool, `{"autoscaler": {"max_containers": 5}}`)
	host := newHost(t, pool)
	queueTasks(t, pool, f, 20, 0)
	readyContainer(t, pool, f, host, time.Minute)
	var queued uuid.UUID
	if err := pool.QueryRow(t.Context(), "select id from tasks where status = 'queued' limit 1").Scan(&queued); err != nil {
		t.Fatal(err)
	}

	page := int32(101)
	listings := []struct {
		name  string
		query string
		args  []any
	}{
		{"tasks", listTasks, []any{f.workspace, nil, false, nil, uuid.Max, page}},
		{"root tasks searched", listTasks, []any{f.workspace, nil, true, "fn", uuid.Max, page}},
		{"app tasks", listAppTasks, []any{nil, false, nil, uuid.Max, page, f.workspace, f.app, nil, nil}},
		{"version tasks", listAppTasks, []any{nil, false, nil, uuid.Max, page, f.workspace, f.app, "fn", 1}},
		{"containers", listContainers, []any{f.workspace, nil, uuid.Max, nil, page}},
		{"app containers", listContainers, []any{f.workspace, "app", uuid.Max, nil, page}},
		{"live containers", listLiveContainers, []any{f.workspace, nil, uuid.Max, nil, page}},
		{"pending facts", pendingFacts, []any{[]uuid.UUID{queued}}},
		{"sandboxes", listSandboxes, []any{f.workspace, uuid.Max, nil, nil, page}},
		{"sandboxes searched", listSandboxes, []any{f.workspace, uuid.Max, nil, "box", page}},
	}
	for _, history := range []int{0, 10_000} {
		if history > 0 {
			addHistory(t, pool, f, host, history)
		}
		exec(t, pool, "analyze")
		for _, listing := range listings {
			plan := explain(t, pool, listing.query, listing.args...)
			t.Logf("%d finished tasks, %s: %s", history, listing.name, summary(plan))
			if history == 0 {
				continue
			}
			for _, table := range []string{"tasks", "attempts", "containers"} {
				if strings.Contains(plan, "Seq Scan on "+table) {
					t.Errorf("%s scans every row of %s:\n%s", listing.name, table, plan)
				}
			}
			// Sandboxes are instances; the workspace's other containers stay unread.
			if strings.HasPrefix(listing.name, "sandboxes") && !strings.Contains(plan, "containers_instances_recent") {
				t.Errorf("%s reads the workspace's container history:\n%s", listing.name, plan)
			}
		}
	}
}
