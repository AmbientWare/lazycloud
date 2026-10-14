package execution

import (
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// Listings read one page, so their cost does not grow with retained
// history: 10,000 more finished tasks on 1,000 more stopped containers leave
// each listing's reads where they were.
func TestListingsReadOnePageOfAnIndex(t *testing.T) {
	t.Parallel()
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
	scans := []scan{
		{"tasks", listTasks, []any{f.workspace, nil, false, nil, uuid.Max, page}},
		{"root tasks", listTasks, []any{f.workspace, nil, true, nil, uuid.Max, page}},
		{"app tasks", listAppTasks, []any{nil, false, nil, uuid.Max, page, f.workspace, f.app, nil, nil}},
		{"version tasks", listAppTasks, []any{nil, false, nil, uuid.Max, page, f.workspace, f.app, "fn", 1}},
		{"containers", listContainers, []any{f.workspace, nil, uuid.Max, nil, page}},
		{"app containers", listContainers, []any{f.workspace, "app", uuid.Max, nil, page}},
		{"live containers", listLiveContainers, []any{f.workspace, nil, uuid.Max, nil, page}},
		{"pending facts", pendingFacts, []any{[]uuid.UUID{queued}}},
		{"sandboxes", listSandboxes, []any{f.workspace, uuid.Max, nil, nil, page}},
		{"sandboxes searched", listSandboxes, []any{f.workspace, uuid.Max, nil, "box", page}},
	}
	addHistory(t, pool, f, host, 1_000)
	exec(t, pool, "analyze")
	small := costAt(t, pool, scans, "1,000 finished tasks", 0)
	addHistory(t, pool, f, host, 10_000)
	exec(t, pool, "analyze")
	large := costAt(t, pool, scans, "11,000 finished tasks", 500)
	constantCost(t, small, large, 20, "10,000 finished tasks and 1,000 stopped containers")
}
