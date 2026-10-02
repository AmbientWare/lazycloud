package observability_test

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/observability"
)

func smallHub() observability.ChangesConfig {
	return observability.ChangesConfig{Retained: 64, Buffer: 8, MaxSubscribers: 10}
}

// Committed task changes reach the workspace's subscribers once per
// statement, in commit order; rolled-back ones and other workspaces'
// changes never do.
func TestChangeStreamDeliversCommittedChangesOfItsWorkspace(t *testing.T) {
	f := newFixture(t, `{"max_pending_tasks": 5000}`)
	otherWS, _, _, _ := f.addFunction("other", "reports", "summarize", `{}`)
	hub, sub := runHub(t, f.pool, smallHub(), f.workspace)
	other, _, err := hub.Subscribe(otherWS, "test", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()

	tasks := f.submit(3)
	_, created := nextEvent(t, sub)
	if created.WorkspaceId != uuid.UUID(f.workspace) || len(created.Changes) != 3 {
		t.Fatalf("created event %+v", created)
	}
	for _, c := range created.Changes {
		if c.Topic != apitypes.ChangeTopicTasks || c.Change != apitypes.ChangeKindCreated || *c.Status != "queued" ||
			*c.AppId != f.app || *c.DeploymentId != f.workload || c.TaskId == nil {
			t.Fatalf("created change %+v", c)
		}
	}

	// A rolled-back write publishes nothing.
	err = pgx.BeginFunc(t.Context(), f.pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(t.Context(), "update tasks set status = 'cancelled' where id = $1", uuid.UUID(tasks[0].ID)); err != nil {
			return err
		}
		return context.Canceled
	})
	if err == nil {
		t.Fatal("the transaction committed")
	}
	if _, err := f.exec.CancelTask(t.Context(), f.workspace, tasks[1].ID); err != nil {
		t.Fatal(err)
	}
	_, cancelled := nextEvent(t, sub)
	if len(cancelled.Changes) != 1 || *cancelled.Changes[0].TaskId != uuid.UUID(tasks[1].ID) ||
		*cancelled.Changes[0].Status != "cancelled" || cancelled.Seq == created.Seq {
		t.Fatalf("cancel event %+v", cancelled)
	}
	noEvent(t, other, 200*time.Millisecond)
}

// A reconnect naming the last event it saw receives what followed; one
// naming an event the hub no longer holds, or a subscriber that falls
// behind, is told to reset.
func TestChangeStreamResumesAfterLastEventIDOrResets(t *testing.T) {
	f := newFixture(t, `{"max_pending_tasks": 5000}`)
	hub, sub := runHub(t, f.pool, smallHub(), f.workspace)

	f.submit(1)
	first, _ := nextEvent(t, sub)
	f.submit(1)
	second, _ := nextEvent(t, sub)
	f.submit(1)
	third, _ := nextEvent(t, sub)

	resumed, resume, err := hub.Subscribe(f.workspace, "test", &first.Seq)
	if err != nil {
		t.Fatal(err)
	}
	defer resumed.Close()
	if resume.Reset || len(resume.Replay) != 2 || resume.Replay[0].Seq != second.Seq || resume.Replay[1].Seq != third.Seq {
		t.Fatalf("resume after the first event: %+v", resume)
	}
	stale := int64(-1)
	_, resume, err = hub.Subscribe(f.workspace, "test", &stale)
	if err != nil {
		t.Fatal(err)
	}
	if !resume.Reset || resume.Latest != third.Seq {
		t.Fatalf("resume from an unknown event: %+v", resume)
	}

	// The subscriber stops reading; nine separate statements overflow its
	// eight-event buffer.
	for range 9 {
		f.submit(1)
	}
	select {
	case <-sub.Reset():
	case <-time.After(10 * time.Second):
		t.Fatal("a subscriber that fell behind was not reset")
	}
	if reason := sub.TakeReset(); reason != observability.ResetBehind {
		t.Fatalf("reset reason %q", reason)
	}
	// The reset covers what was queued; later changes flow again.
	select {
	case e := <-sub.Events():
		t.Fatalf("an event the reset covers was delivered: %s", e.Frame)
	default:
	}
	f.submit(1)
	nextEvent(t, sub)
}

// A statement that changes thousands of rows sends one notification under
// PostgreSQL's 8000-byte limit, grouped with a count.
func TestLargeStatementsPublishGroupedChanges(t *testing.T) {
	f := newFixture(t, `{"max_pending_tasks": 5000}`)
	_, sub := runHub(t, f.pool, smallHub(), f.workspace)

	f.submit(2000)
	e, event := nextEvent(t, sub)
	if len(e.Frame) >= 8000 || len(event.Changes) != 1 {
		t.Fatalf("grouped event of %d bytes: %+v", len(e.Frame), event)
	}
	c := event.Changes[0]
	if c.Topic != apitypes.ChangeTopicTasks || c.Count == nil || *c.Count != 2000 || c.ResourceId != nil ||
		*c.DeploymentId != f.workload {
		t.Fatalf("grouped change %+v", c)
	}

	// An update of thousands of rows groups before building items too.
	f.exec1("update tasks set status = 'cancelled', finished_at = now() where workload_id = $1", f.workload)
	e, event = nextEvent(t, sub)
	if c := event.Changes[0]; len(e.Frame) >= 8000 || c.Count == nil || *c.Count != 2000 || c.Change != apitypes.ChangeKindUpdated {
		t.Fatalf("grouped update %+v", event)
	}
}

// Containers, apps and deployments publish their state changes too.
func TestContainerAppAndDeploymentChangesArePublished(t *testing.T) {
	f := newFixture(t, `{"max_pending_tasks": 100}`)
	_, sub := runHub(t, f.pool, smallHub(), f.workspace)

	_, container := f.placedContainer(f.release)
	_, event := nextEvent(t, sub)
	if c := event.Changes[0]; c.Topic != apitypes.ChangeTopicContainers || *c.ContainerId != uuid.UUID(container) || *c.Status != "ready" {
		t.Fatalf("container change %+v", event)
	}
	if _, err := f.exec.StopContainer(t.Context(), f.workspace, container); err != nil {
		t.Fatal(err)
	}
	_, event = nextEvent(t, sub)
	if c := event.Changes[0]; c.Topic != apitypes.ChangeTopicContainers || *c.Status != string(execution.ContainerDraining) {
		t.Fatalf("drain change %+v", event)
	}
	f.exec1("update apps set state = 'paused' where id = $1", f.app)
	_, event = nextEvent(t, sub)
	if c := event.Changes[0]; c.Topic != apitypes.ChangeTopicApps || *c.Status != "paused" || *c.AppId != f.app {
		t.Fatalf("app change %+v", event)
	}
	f.exec1("update workloads set desired_state = 'deleted' where id = $1", f.workload)
	_, event = nextEvent(t, sub)
	if c := event.Changes[0]; c.Topic != apitypes.ChangeTopicDeployments || c.Change != apitypes.ChangeKindDeleted {
		t.Fatalf("deployment change %+v", event)
	}
	// Writes that change no published state send nothing.
	f.exec1("update workloads set next_version = next_version + 1 where id = $1", f.workload)
	noEvent(t, sub, 200*time.Millisecond)
}

// A claim sends no notification, so claims never queue on PostgreSQL's
// notification lock; the started tasks reach the stream coalesced, with
// their current status.
func TestClaimsPublishStartedTasksOffTheClaimTransaction(t *testing.T) {
	f := newFixture(t, `{"max_pending_tasks": 100}`)
	_, sub := runHub(t, f.pool, smallHub(), f.workspace)
	host, container := f.placedContainer(f.release)
	nextEvent(t, sub) // the container
	task := f.submit(1)[0]
	nextEvent(t, sub) // the submit
	claimed := f.claim(host, container)
	noEvent(t, sub, 300*time.Millisecond)

	ctx, stop := context.WithCancel(t.Context())
	defer stop()
	go func() { _ = f.obs.RunStartedPublisher(ctx) }()
	f.obs.TasksStarted([]execution.TaskID{claimed.Task})
	_, started := nextEvent(t, sub)
	if c := started.Changes[0]; *c.TaskId != uuid.UUID(task.ID) || *c.Status != "running" {
		t.Fatalf("started change %+v", started)
	}
}

// Volumes and metered usage publish too: a created, measured or deleted
// volume, and one usage change per workspace for each batch of charges.
func TestVolumeAndUsageChangesArePublished(t *testing.T) {
	f := newFixture(t, `{}`)
	_, sub := runHub(t, f.pool, smallHub(), f.workspace)

	var volume uuid.UUID
	if err := f.pool.QueryRow(t.Context(), "insert into volumes (workspace_id, name) values ($1, 'data') returning id", uuid.UUID(f.workspace)).Scan(&volume); err != nil {
		t.Fatal(err)
	}
	_, event := nextEvent(t, sub)
	if c := event.Changes[0]; c.Topic != apitypes.ChangeTopicStorageVolumes || c.Change != apitypes.ChangeKindCreated || *c.ResourceId != volume.String() {
		t.Fatalf("volume created %+v", event)
	}
	f.exec1("update volumes set size_bytes = 4096, size_measured_at = now() where id = $1", volume)
	if _, event = nextEvent(t, sub); event.Changes[0].Change != apitypes.ChangeKindUpdated {
		t.Fatalf("volume measured %+v", event)
	}
	f.exec1("update volumes set state = 'deleting', deleted_at = now() where id = $1", volume)
	if _, event = nextEvent(t, sub); event.Changes[0].Change != apitypes.ChangeKindDeleted {
		t.Fatalf("volume deleted %+v", event)
	}

	f.exec1(`insert into ledger_entries (source_kind, source_id, started_at, ended_at, user_id, workspace_id, billing_owner, rate_class,
                            gpu_count, cpu_millis, memory_bytes, pricing_version, container_nanos, cpu_nanos, memory_nanos, gpu_nanos)
select 'container', gen_random_uuid(), now() - interval '1 minute' * g, now() - interval '1 minute' * (g - 1), m.user_id, $1,
       'platform_fleet', 'auto', 0, 1000, 1 << 30, 'v1', 1, 1, 1, 0
from workspace_members m, generate_series(1, 3) g where m.workspace_id = $1`, uuid.UUID(f.workspace))
	_, event = nextEvent(t, sub)
	if len(event.Changes) != 1 || event.Changes[0].Topic != apitypes.ChangeTopicUsage || *event.Changes[0].Count != 3 {
		t.Fatalf("usage change %+v", event)
	}
}
