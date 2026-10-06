package hostsession_test

import (
	"slices"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// A start that needs the managed image waits while another server converts
// it, its syncs writing nothing and building nothing, and is sent with the
// converted copy once that server records it. The wait is the start's
// conversion stage.
func TestStartWaitsForTheManagedImageConversion(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	recordManagedSource(t, h.pool, "3.11")
	h.exec(`insert into platform_images (reference, architecture, lease_token, leased_until)
		values ($1, 'amd64', gen_random_uuid(), now() + interval '1 hour')`, managedSource("3.11"))
	ws, container := h.startingContainerWith(host, `{"handler": "reports:summarize", "image": {"python_version": "3.11"}}`)
	stream := open(t, ctx, h.client)

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

	convertManagedImage(t, h.pool, "3.11")
	select {
	case image := <-got:
		if image != managedReference("3.11") {
			t.Fatalf("the start pulls %s, want %s", image, managedReference("3.11"))
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the start was not sent after its image was converted")
	}

	deadline := time.Now().Add(5 * time.Second)
	for {
		lifecycle, err := h.obs.ContainerLifecycle(t.Context(), ws, container)
		if err != nil {
			t.Fatal(err)
		}
		i := slices.IndexFunc(lifecycle.Stages, func(s apitypes.LifecycleStage) bool {
			return s.Stage == apitypes.LifecycleStageKindConversion
		})
		if i >= 0 {
			if ms := *lifecycle.Stages[i].DurationMs; ms < 900 {
				t.Fatalf("the conversion stage lasted %d ms of a wait of about a second", ms)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("no conversion stage: %+v", lifecycle.Stages)
		}
		time.Sleep(50 * time.Millisecond)
	}
}
