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
		{"tasks", listTasks, []any{f.workspace, uuid.Max, page}},
		{"root tasks", listWorkloadTasks, taskListing{rootOnly: true}.args(f)},
		{"failed tasks", listWorkloadTasks, taskListing{status: TaskFailed}.args(f)},
		{"app tasks", listWorkloadTasks, taskListing{app: f.app}.args(f)},
		{"root app tasks", listWorkloadTasks, taskListing{app: f.app, rootOnly: true}.args(f)},
		{"failed app tasks", listWorkloadTasks, taskListing{app: f.app, status: TaskFailed}.args(f)},
		{"version tasks", listWorkloadTasks, taskListing{app: f.app, function: "f", version: 1}.args(f)},
		{"tasks searched by function", listWorkloadTasks, taskListing{search: "f"}.args(f)},
		{"tasks searched by id", listWorkloadTasks, taskListing{search: queued.String()[:8]}.args(f)},
		{"tasks searched for nothing", listWorkloadTasks, taskListing{search: "zz"}.args(f)},
		{"app tasks searched for nothing", listWorkloadTasks, taskListing{app: f.app, search: "zz"}.args(f)},
		{"containers", listContainers, []any{f.workspace, nil, uuid.Max, nil, page}},
		{"app containers", listContainers, []any{f.workspace, "app", uuid.Max, nil, page}},
		{"live containers", listLiveContainers, []any{f.workspace, nil, uuid.Max, nil, page}},
		{"pending facts", pendingFacts, []any{[]uuid.UUID{queued}}},
		{"sandboxes", listSandboxes, []any{f.workspace, uuid.Max, nil, nil, page}},
		{"sandboxes searched", listSandboxes, []any{f.workspace, uuid.Max, nil, "box", page}},
	}
	// Finished tasks are a queued task's children, which root listings skip.
	history := func(tasks int) {
		addHistory(t, pool, f, host, tasks)
		exec(t, pool, "update tasks set parent_task_id = $1, root_task_id = $1 where status = 'succeeded' and parent_task_id is null", queued)
		exec(t, pool, "analyze")
	}
	history(1_000)
	small := costAt(t, pool, scans, "1,000 finished tasks", 0)
	history(10_000)
	large := costAt(t, pool, scans, "11,000 finished tasks", 500)
	constantCost(t, small, large, "10,000 finished tasks and 1,000 stopped containers")
}

// taskListing is a ListWorkloadTasks filter.
type taskListing struct {
	app, function, version, search any
	status                         TaskStatus
	rootOnly                       bool
}

// args are the filter's arguments for a first page of 101.
func (l taskListing) args(f releaseFixture) []any {
	statuses := []string{"queued", "running", "succeeded", "failed", "cancelled"}
	if l.status != "" {
		statuses = []string{string(l.status)}
	}
	roots := []bool{true, false}
	if l.rootOnly {
		roots = []bool{true}
	}
	low, high := uuid.Nil, uuid.Max
	if l.search != nil {
		low, high = idPrefixRange(l.search.(string))
	}
	return []any{statuses, roots, l.search, low, high, uuid.Max, int32(101), f.workspace, l.app, l.function, l.version}
}
