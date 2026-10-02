package database_test

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// A committed NOTIFY wakes subscribers of its payload and of the channel.
func TestListenerWakesOnCommittedNotify(t *testing.T) {
	pool := dbtest.New(t)
	listener := database.NewListener(pool, slog.New(slog.NewTextHandler(io.Discard, nil)), database.ChannelExecution)
	byPayload, cancelPayload := listener.Subscribe(database.ChannelExecution, "release-1")
	defer cancelPayload()
	anyPayload, cancelAny := listener.Subscribe(database.ChannelExecution, "")
	defer cancelAny()

	ctx, cancel := context.WithCancel(t.Context())
	var wg sync.WaitGroup
	wg.Go(func() {
		if err := listener.Run(ctx); err != nil {
			t.Error(err)
		}
	})
	defer wg.Wait()
	defer cancel()

	// Run wakes everyone once LISTEN is active.
	for _, wake := range []<-chan struct{}{byPayload, anyPayload} {
		select {
		case <-wake:
		case <-time.After(5 * time.Second):
			t.Fatal("no wake after LISTEN")
		}
	}
	err := pgx.BeginFunc(t.Context(), pool, func(tx pgx.Tx) error {
		return database.Notify(t.Context(), tx, database.ChannelExecution, "release-1")
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, wake := range []<-chan struct{}{byPayload, anyPayload} {
		select {
		case <-wake:
		case <-time.After(5 * time.Second):
			t.Fatal("no wake after a committed notify")
		}
	}
}
