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
	other, _, err := hub.Subscribe(otherWS, nil)
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

	resumed, resume, err := hub.Subscribe(f.workspace, &first.Seq)
	if err != nil {
		t.Fatal(err)
	}
	defer resumed.Close()
	if resume.Reset || len(resume.Replay) != 2 || resume.Replay[0].Seq != second.Seq || resume.Replay[1].Seq != third.Seq {
		t.Fatalf("resume after the first event: %+v", resume)
	}
	stale := int64(-1)
	_, resume, err = hub.Subscribe(f.workspace, &stale)
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
