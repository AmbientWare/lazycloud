package execution

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// A checkpoint-enabled release gets one automatic snapshot, of its oldest
// ready container, and that container's host is woken to take it.
func TestAutomaticSnapshotTakesTheOldestReadyContainerOnce(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := newRelease(t, pool, `{"checkpoint": {}}`)
	host := newHost(t, pool)
	oldest := readyContainer(t, pool, f, host, time.Minute)
	readyContainer(t, pool, f, host, time.Second)
	plain := newRelease(t, pool, `{}`)
	readyContainer(t, pool, plain, host, time.Minute)

	listen, err := pool.Acquire(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer listen.Release()
	if _, err := listen.Exec(t.Context(), "listen lc_host"); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := e.TakeAutomaticSnapshots(t.Context(), discardLogger()); err != nil {
			t.Fatal(err)
		}
	}
	var snapshots int
	var container uuid.UUID
	if err := pool.QueryRow(t.Context(), `
select count(*), min(container_id::text)::uuid from memory_snapshots where automatic and state = 'pending'`).Scan(&snapshots, &container); err != nil {
		t.Fatal(err)
	}
	if snapshots != 1 || container != oldest {
		t.Fatalf("%d pending snapshots of %s, want one of the oldest container %s", snapshots, container, oldest)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	notification, err := listen.Conn().WaitForNotification(ctx)
	if err != nil || notification.Payload != host.String() {
		t.Fatalf("host wake: %v %v; want lc_host for the container's host", notification, err)
	}
}
