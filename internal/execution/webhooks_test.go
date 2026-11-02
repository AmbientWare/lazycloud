package execution

import (
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// A release that stops starting announces workload.failed once, and the
// queued tasks it fails announce task.failed, to the app's webhooks.
func TestAReleaseThatStopsStartingAnnouncesItOnce(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"resources": {"cpu_millis": 500, "memory_mib": 256}, "max_pending_tasks": 10}`)
	exec(t, pool, `insert into webhooks (workspace_id, app_name, url, events)
values ($1, 'reports', 'https://example.com/hook', array['task.failed', 'workload.failed'])`, uuid.UUID(f.workspace))
	task := submit(t, e, f, 1)[0]

	for range startFailureLimit + 1 {
		host, container := placedContainer(t, pool, f, ContainerStarting, 1)
		exited(t, e, uuid.UUID(host), container, ContainerExit{Reason: StopCrashed, Message: "exit code 1"})
	}

	rows, err := pool.Query(t.Context(), "select event, data from webhook_deliveries order by id")
	if err != nil {
		t.Fatal(err)
	}
	type delivery struct {
		event string
		data  map[string]any
	}
	var got []delivery
	for rows.Next() {
		var d delivery
		if err := rows.Scan(&d.event, &d.data); err != nil {
			t.Fatal(err)
		}
		got = append(got, d)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0].event != "workload.failed" || got[1].event != "task.failed" {
		t.Fatalf("deliveries %+v, want one workload.failed and one task.failed", got)
	}
	if got[0].data["reason"] != string(StopStartFailed) || got[0].data["message"] != "exit code 1" || got[0].data["app"] != "reports" {
		t.Fatalf("workload.failed data %v", got[0].data)
	}
	if got[1].data["task_id"] != uuid.UUID(task.ID).String() || got[1].data["status"] != "failed" {
		t.Fatalf("task.failed data %v", got[1].data)
	}
}
