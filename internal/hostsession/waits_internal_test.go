package hostsession

import (
	"context"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/images"
)

// A wake-up the old subscription holds when a watch changes its keys, as a
// confirmation committed while a grant's layers were read, is reported
// rather than lost.
func TestReplacingAWatchKeepsItsWakeUp(t *testing.T) {
	pool := dbtest.New(t)
	listener := database.NewListener(pool, slog.New(slog.DiscardHandler), database.ChannelImageBuild)
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Go(func() { _ = listener.Run(ctx) })
	t.Cleanup(func() { cancel(); wg.Wait() })

	var w watch
	defer w.set(listener)
	w.set(listener, "") // every key
	// The listener wakes everyone once it starts listening.
	time.Sleep(200 * time.Millisecond)
	select {
	case <-w.wake:
	default:
	}
	key := images.ReplicasConfirmed("us-east-2")
	if _, err := pool.Exec(t.Context(), "select pg_notify($1, $2)", string(database.ChannelImageBuild), key); err != nil {
		t.Fatal(err)
	}
	for deadline := time.Now().Add(3 * time.Second); len(w.wake) == 0; time.Sleep(10 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("the notification did not arrive")
		}
	}
	if !w.set(listener, key) {
		t.Fatal("the wake-up the replaced subscription held was lost")
	}
	if w.set(listener, key) {
		t.Fatal("an unchanged watch reported a wake-up")
	}
}
