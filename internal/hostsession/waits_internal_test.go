package hostsession

import (
	"context"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// A session waiting for one image build wakes when that build changes, not
// when any other does.
func TestBuildWaitsWakeOnlyForTheirBuilds(t *testing.T) {
	pool := dbtest.New(t)
	listener := database.NewListener(pool, slog.New(slog.DiscardHandler), database.ChannelImageBuild)
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Go(func() { _ = listener.Run(ctx) })
	t.Cleanup(func() { cancel(); wg.Wait() })

	waits := buildWaits{wake: make(chan struct{}, 1), subs: map[uuid.UUID]func(){}}
	defer waits.close()
	awaited, other := uuid.New(), uuid.New()
	waits.await(listener, map[uuid.UUID]bool{awaited: true})
	// The listener wakes everyone once it starts listening.
	time.Sleep(200 * time.Millisecond)
	select {
	case <-waits.wake:
	default:
	}
	notify := func(build uuid.UUID) {
		if _, err := pool.Exec(t.Context(), "select pg_notify($1, $2)", string(database.ChannelImageBuild), build.String()); err != nil {
			t.Fatal(err)
		}
	}
	notify(other)
	select {
	case <-waits.wake:
		t.Fatal("another build's change woke the session")
	case <-time.After(300 * time.Millisecond):
	}
	notify(awaited)
	select {
	case <-waits.wake:
	case <-time.After(3 * time.Second):
		t.Fatal("the awaited build's change did not wake the session")
	}
	waits.await(listener, map[uuid.UUID]bool{})
	if len(waits.subs) != 0 {
		t.Fatal("a build no start waits for stays subscribed")
	}
}
