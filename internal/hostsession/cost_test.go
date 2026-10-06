package hostsession_test

import (
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// replicate adds n starting containers of container's release on its host.
func (h *harness) replicate(container uuid.UUID, n int) {
	h.t.Helper()
	h.exec(`
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
select workspace_id, release_id, 'starting', host_id, slots, cpu_millis, memory_bytes, now()
from containers, generate_series(1, $2) where id = $1`, container, n)
}

// TestImageQueriesPerSyncDoNotGrowWithReplicas: the queries that resolve
// and grant a start's image run once per image and sync, however many
// replicas start, and a start that waits for its image's conversion reads
// no secrets.
func TestImageQueriesPerSyncDoNotGrowWithReplicas(t *testing.T) {
	h, _, counter := countedHarness(t)
	host, ctx := h.enroll()
	const replicas = 4
	ref := reference("a")
	h.publish(ref, h.storeLayer("a"))
	converted := h.startingImage(host, ref)
	h.replicate(converted, replicas-1)
	_, managed := h.startingWith(host, `{"handler": "reports:summarize", "image": {"python_version": "3.12"}}`)
	h.replicate(managed, replicas-1)
	old := reference("old")
	waitingWS, waiting := h.startingWith(host, `{"handler": "reports:summarize", "secrets": ["TOKEN"],
		"image": {"python_version": "3.12", "image_id": "img_0123456789abcdef01234567", "reference": "`+old+`"}}`)
	if _, err := h.secrets.Set(t.Context(), waitingWS, "TOKEN", "hunter2-hunter2"); err != nil {
		t.Fatal(err)
	}
	h.exec(`insert into images (digest, id, dockerfile, python_version, architecture, reference, ready_at)
		values (sha256('unconverted'), 'img_0123456789abcdef01234567', 'FROM scratch', '3.12', 'amd64', $1, now())`, old)
	h.replicate(waiting, replicas-1)

	counter.take()
	in := commands(t, open(t, ctx, h.client))
	for range 2 * replicas {
		next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStart() != nil })
	}
	// The sync that sends the starts resolves each image once: the
	// workspace image's architecture, the managed image's pin, the host's
	// architecture and the platform copy, and one signing per image.
	first := counter.take()
	for name, want := range map[string]int{"ImageArchitecture": 1, "ManagedSource": 1, "HostArchitecture": 1, "PlatformImagesOf": 1, "LayerReadsFor": 2} {
		if first[name] != want {
			t.Errorf("sending %d starts ran %s %d times, want %d", 2*replicas, name, first[name], want)
		}
	}
	// Later syncs look once at the image the waiting replicas need, and
	// read none of their secrets.
	time.Sleep(time.Second)
	later := counter.take()
	syncs := later["StartingContainersOnHost"]
	if syncs == 0 {
		t.Fatal("no sync ran")
	}
	for _, name := range []string{"ReferenceLayers", "ImageRuntime", "LatestBuild"} {
		// A sync may straddle the window's start.
		if later[name] > syncs+1 {
			t.Errorf("%d syncs ran %s %d times for %d waiting replicas", syncs, name, later[name], replicas)
		}
	}
	if n := first["SealedSecrets"] + later["SealedSecrets"]; n != 0 {
		t.Errorf("waiting starts read their secrets %d times", n)
	}
}
