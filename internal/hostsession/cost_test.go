package hostsession_test

import (
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/hostsession"
)

// replicate adds n starting containers of container's release on its host.
func (h *harness) replicate(container uuid.UUID, n int) {
	h.t.Helper()
	h.exec(`
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
select workspace_id, release_id, 'starting', host_id, slots, cpu_millis, memory_bytes, now()
from containers, generate_series(1, $2) where id = $1`, container, n)
}

// TestImageQueriesPerSyncDoNotGrowWithReplicas: a sync that waits for an
// image's conversion runs as many statements for four replicas of each
// image as for one, their secrets among them, and sending the starts
// adds only a fixed cost per container.
func TestImageQueriesPerSyncDoNotGrowWithReplicas(t *testing.T) {
	sending1, waiting1 := syncStatements(t, 1)
	sending2, _ := syncStatements(t, 2)
	sending4, waiting4 := syncStatements(t, 4)
	t.Logf("sending 1, 2 and 4 replicas: %d, %d and %d statements; waiting: %d and %d", sending1, sending2, sending4, waiting1, waiting4)
	if waiting4 != waiting1 {
		t.Errorf("a waiting sync ran %d statements for one replica and %d for four", waiting1, waiting4)
	}
	if perReplica := sending2 - sending1; sending4-sending2 != 2*perReplica {
		t.Errorf("sending starts grew by %d statements from one replica to two and by %d from two to four", perReplica, sending4-sending2)
	}
}

// syncStatements opens a session on a host starting replicas of each of
// three images, a converted one, the managed one and one that waits for its
// conversion, and returns the statements the sync that sends the starts
// ran and those of a later sync while the waiting replicas wait.
func syncStatements(t *testing.T, replicas int) (sending, waiting int64) {
	t.Helper()
	// Syncs run only on the session's open and the waits' wake-ups.
	h, base, counter := countedHarness(t, func(c *hostsession.Config) { c.TouchInterval = time.Hour })
	host, ctx := h.enroll()
	ref := reference("a")
	h.publish(ref, h.storeLayer("a"))
	h.replicate(h.startingImage(host, ref), replicas-1)
	_, managed := h.startingWith(host, `{"handler": "reports:summarize", "image": {"python_version": "3.12"}}`)
	h.replicate(managed, replicas-1)
	old := reference("old")
	waiterWS, waiter := h.startingWith(host, `{"handler": "reports:summarize", "secrets": ["TOKEN"],
		"image": {"python_version": "3.12", "image_id": "img_0123456789abcdef01234567", "reference": "`+old+`"}}`)
	if _, err := h.secrets.Set(t.Context(), waiterWS, "TOKEN", "hunter2-hunter2"); err != nil {
		t.Fatal(err)
	}
	h.exec(`insert into images (digest, id, dockerfile, python_version, architecture, reference, ready_at)
		values (sha256('unconverted'), 'img_0123456789abcdef01234567', 'FROM scratch', '3.12', 'amd64', $1, now())`, old)
	h.replicate(waiter, replicas-1)

	counter.settle()
	in := commands(t, open(t, ctx, h.client))
	for range 2 * replicas {
		next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStart() != nil })
	}
	sending = counter.settle()
	var build uuid.UUID
	if err := base.QueryRow(t.Context(), "select id from image_builds").Scan(&build); err != nil {
		t.Fatal(err)
	}
	if _, err := base.Exec(t.Context(), "select pg_notify($1, $2)", string(database.ChannelImageBuild), build.String()); err != nil {
		t.Fatal(err)
	}
	waiting = counter.settle()
	if waiting == 0 {
		t.Fatal("the waiting replicas' build woke no sync")
	}
	return sending, waiting
}
