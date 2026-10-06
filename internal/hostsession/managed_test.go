package hostsession_test

import (
	"context"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// A start that needs the managed image waits while another server converts
// it, its syncs writing nothing and building nothing, and is sent with the
// converted copy once that server records it. The wait is the start's
// conversion stage from the container's assignment: stored up to the end of
// the session that ended during it, then until the start is sent.
func TestStartWaitsForTheManagedImageConversion(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	recordManagedSource(t, h.pool, "3.11")
	h.exec(`insert into platform_images (reference, architecture, lease_token, leased_until)
		values ($1, 'amd64', gen_random_uuid(), now() + interval '1 hour')`, managedSource("3.11"))
	ws, container := h.startingContainerWith(host, `{"handler": "reports:summarize", "image": {"python_version": "3.11"}}`)
	sent := func(stream hostStream) <-chan string {
		got := make(chan string, 1)
		go func() {
			for {
				msg, err := stream.Recv()
				if err != nil {
					return
				}
				if start := msg.GetStart(); start.GetContainerId() == container.String() {
					got <- start.GetImage()
					return
				}
			}
		}()
		return got
	}
	first, endFirst := context.WithCancel(ctx)
	defer endFirst()
	got := sent(open(t, first, h.client))
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
	select {
	case image := <-got:
		t.Fatalf("the start was sent with %s before its image was converted", image)
	case <-time.After(time.Second):
	}
	if after := versions(); after != before {
		t.Fatalf("waiting syncs rewrote image rows: %s, then %s", before, after)
	}
	if n := h.count("select count(*) from image_builds"); n != 0 {
		t.Fatalf("waiting syncs started %d builds", n)
	}

	endFirst()
	if waited := h.conversionStage(ws, container, func(apitypes.LifecycleStage) bool { return true }); *waited.DurationMs < 900 {
		t.Fatalf("the ended session stored %d ms of a wait of over a second", *waited.DurationMs)
	}
	reopened := time.Now()
	got = sent(open(t, ctx, h.client))
	select {
	case image := <-got:
		t.Fatalf("the next session sent the start with %s before its image was converted", image)
	case <-time.After(500 * time.Millisecond):
	}
	convertManagedImage(t, h.pool, "3.11")
	select {
	case image := <-got:
		if image != managedReference("3.11") {
			t.Fatalf("the start pulls %s, want %s", image, managedReference("3.11"))
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the start was not sent after its image was converted")
	}

	stage := h.conversionStage(ws, container, func(s apitypes.LifecycleStage) bool { return s.FinishedAt.After(reopened) })
	if *stage.DurationMs < 1500 {
		t.Fatalf("the conversion stage lasted %d ms of a wait of over 1.5 seconds", *stage.DurationMs)
	}
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
