package hostsession_test

import (
	"context"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// A start that needs the managed image waits while another server converts
// it, its syncs writing nothing and building nothing, and is sent with the
// converted copy once that server records it. The wait is the start's
// conversion stage from the container's assignment: stored up to the end of
// the session that ended during it, then until the start is sent.
func TestStartWaitsForTheManagedImageConversion(t *testing.T) {
	t.Parallel()
	h := start(t)
	host, ctx := h.enroll()
	ws, container := h.waitingManagedStart(host)
	isStart := func(m *hostproto.ServerMessage) bool { return m.GetStart().GetContainerId() == container.String() }
	versions := func() string {
		var v string
		if err := h.pool.QueryRow(t.Context(), `
select string_agg(xmin::text, ',') from platform_images
union all select string_agg(xmin::text, ',') from managed_images`).Scan(&v); err != nil {
			t.Fatal(err)
		}
		return v
	}
	before := versions()
	first, endFirst := context.WithCancel(ctx)
	defer endFirst()
	in := commands(t, open(t, first, h.client))
	if slices.ContainsFunc(h.synced(host, in), isStart) {
		t.Fatal("the start was sent before its image was converted")
	}
	if after := versions(); after != before {
		t.Fatalf("waiting syncs rewrote image rows: %s, then %s", before, after)
	}
	if n := h.count("select count(*) from image_builds"); n != 0 {
		t.Fatalf("waiting syncs started %d builds", n)
	}

	endFirst()
	if waited := h.conversionStage(ws, container, func(apitypes.LifecycleStage) bool { return true }); *waited.DurationMs < assignedAgo.Milliseconds() {
		t.Fatalf("the ended session stored %d ms of a wait since the assignment %s ago", *waited.DurationMs, assignedAgo)
	}
	reopened := time.Now()
	in = commands(t, open(t, ctx, h.client))
	if slices.ContainsFunc(h.synced(host, in), isStart) {
		t.Fatal("the next session sent the start before its image was converted")
	}
	convertManagedImage(t, h.pool, "3.11")
	if image := next(t, in, 5*time.Second, isStart).GetStart().GetImage(); image != managedReference("3.11") {
		t.Fatalf("the start pulls %s, want %s", image, managedReference("3.11"))
	}
	h.conversionStage(ws, container, func(s apitypes.LifecycleStage) bool { return s.FinishedAt.After(reopened) })
}

// conversionStage waits for container's conversion stage to begin at its
// assignment and satisfy done.
func (h *harness) conversionStage(ws identity.WorkspaceID, container execution.ContainerID, done func(apitypes.LifecycleStage) bool) apitypes.LifecycleStage {
	h.t.Helper()
	var assigned time.Time
	if err := h.pool.QueryRow(h.t.Context(), "select assigned_at from containers where id = $1", uuid.UUID(container)).Scan(&assigned); err != nil {
		h.t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		lifecycle, err := h.obs.ContainerLifecycle(h.t.Context(), ws, container)
		if err != nil {
			h.t.Fatal(err)
		}
		i := slices.IndexFunc(lifecycle.Stages, func(s apitypes.LifecycleStage) bool {
			return s.Stage == apitypes.LifecycleStageKindConversion
		})
		if i >= 0 && !lifecycle.Stages[i].StartedAt.Equal(assigned) {
			h.t.Fatalf("the conversion stage began at %s, assigned at %s", lifecycle.Stages[i].StartedAt, assigned)
		}
		if i >= 0 && done(lifecycle.Stages[i]) {
			return lifecycle.Stages[i]
		}
		if time.Now().After(deadline) {
			h.t.Fatalf("no conversion stage: %+v", lifecycle.Stages)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// assignedAgo is how long before the test a waiting start was assigned.
const assignedAgo = 10 * time.Second

// waitingManagedStart is a starting container on host, assigned
// assignedAgo, whose managed image another server is converting.
func (h *harness) waitingManagedStart(host compute.HostID) (identity.WorkspaceID, execution.ContainerID) {
	h.t.Helper()
	recordManagedSource(h.t, h.pool, "3.11")
	h.exec(`insert into platform_images (reference, architecture, lease_token, leased_until)
		values ($1, 'amd64', gen_random_uuid(), now() + interval '1 hour')`, managedSource("3.11"))
	ws, container := h.startingContainerWith(host, `{"handler": "reports:summarize", "image": {"python_version": "3.11"}}`)
	h.exec("update containers set assigned_at = now() - make_interval(secs => $2) where id = $1", uuid.UUID(container), assignedAgo.Seconds())
	return ws, container
}

// synced returns once the session whose commands in carries finished a
// sync that began after the call, with the commands it received meanwhile.
// It starts a container and, once that is sent, another: a later sync
// sends the second.
func (h *harness) synced(host compute.HostID, in <-chan *hostproto.ServerMessage) []*hostproto.ServerMessage {
	h.t.Helper()
	var seen []*hostproto.ServerMessage
	for range 2 {
		_, marker := h.startingWith(host, `{"handler": "reports:summarize", "image": {"python_version": "3.12"}}`)
		next(h.t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool {
			seen = append(seen, m)
			return m.GetStart().GetContainerId() == marker.String()
		})
	}
	return seen
}

// A start that waited in a session that ended, sent by the next one with
// no wait, has a conversion stage that ends at the send, not at the
// disconnect.
func TestAWaitEndsWhereItsStartIsSent(t *testing.T) {
	t.Parallel()
	h := start(t)
	host, ctx := h.enroll()
	ws, container := h.waitingManagedStart(host)
	first, endFirst := context.WithCancel(ctx)
	defer endFirst()
	h.synced(host, commands(t, open(t, first, h.client)))
	endFirst()
	h.conversionStage(ws, container, func(apitypes.LifecycleStage) bool { return true })
	convertManagedImage(t, h.pool, "3.11")
	reopened := time.Now()
	in := commands(t, open(t, ctx, h.client))
	next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStart().GetContainerId() == container.String() })
	h.conversionStage(ws, container, func(s apitypes.LifecycleStage) bool { return s.FinishedAt.After(reopened) })
}

// A start whose wait ended but whose image reference cannot be recorded
// ends the session and keeps its wait as its conversion stage.
func TestAWaitSurvivesAFailedSend(t *testing.T) {
	t.Parallel()
	h := start(t)
	host, ctx := h.enroll()
	ws, container := h.waitingManagedStart(host)
	in := commands(t, open(t, ctx, h.client))
	h.synced(host, in)
	h.exec(`create function refuse_image_reference() returns trigger language plpgsql as $$
begin raise exception 'refused'; end $$`)
	h.exec(`create trigger refuse_image_reference before update of image_reference on containers
		for each row execute function refuse_image_reference()`)
	convertManagedImage(t, h.pool, "3.11")
	for range in {
	}
	h.conversionStage(ws, container, func(s apitypes.LifecycleStage) bool { return *s.DurationMs >= assignedAgo.Milliseconds() })
}
