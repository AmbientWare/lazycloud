package database_test

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// run starts listener until the test ends.
func run(t *testing.T, listener *database.Listener) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Go(func() {
		if err := listener.Run(ctx); err != nil {
			t.Error(err)
		}
	})
	t.Cleanup(func() {
		cancel()
		wg.Wait()
	})
}

// woken waits for a wake on each channel.
func woken(t *testing.T, step string, wakes ...<-chan struct{}) {
	t.Helper()
	for _, wake := range wakes {
		select {
		case <-wake:
		case <-time.After(5 * time.Second):
			t.Fatalf("%s: no wake", step)
		}
	}
}

func notifyAll(t *testing.T, pool *pgxpool.Pool, channel database.Channel, payloads ...string) {
	t.Helper()
	if err := pgx.BeginFunc(t.Context(), pool, func(tx pgx.Tx) error {
		return database.NotifyAll(t.Context(), tx, channel, payloads)
	}); err != nil {
		t.Fatal(err)
	}
}

// A committed NotifyAll packs many keys into few notifications, and each key
// still wakes its own subscribers, and the channel's, and no others, even a
// queue key with a comma.
func TestNotifyAllWakesEveryPackedID(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	listener := database.NewListener(pool, slog.New(slog.NewTextHandler(io.Discard, nil)), database.ChannelTask)
	ids := make([]string, 500)
	for n := range ids {
		ids[n] = uuid.NewString()
	}
	ids[0] += ":jobs,urgent"
	first, cancelFirst := listener.Subscribe(database.ChannelTask, ids[0])
	defer cancelFirst()
	last, cancelLast := listener.Subscribe(database.ChannelTask, ids[len(ids)-1])
	defer cancelLast()
	anyPayload, cancelAny := listener.Subscribe(database.ChannelTask, "")
	defer cancelAny()
	other, cancelOther := listener.Subscribe(database.ChannelTask, uuid.NewString())
	defer cancelOther()
	sentinel := uuid.NewString()
	after, cancelAfter := listener.Subscribe(database.ChannelTask, sentinel)
	defer cancelAfter()

	run(t, listener)
	// Run wakes everyone once LISTEN is active.
	woken(t, "after LISTEN", first, last, anyPayload, other, after)

	notifyAll(t, pool, database.ChannelTask, append(ids, ids[0])...)
	woken(t, "after a committed notify", first, last, anyPayload)
	// Notifications arrive in commit order, so once a later commit's wake
	// arrives, every wake of the first has.
	notifyAll(t, pool, database.ChannelTask, sentinel)
	woken(t, "after the sentinel", after)
	select {
	case <-other:
		t.Fatal("an id outside the notification woke its subscriber")
	default:
	}
}

// Notifications sent while the listener is disconnected are lost, so a lost
// connection wakes every subscriber to re-read; the listener then connects
// again and delivers later notifications.
func TestListenerWakesEveryoneWhenItsConnectionDrops(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	listener := database.NewListener(pool, slog.New(slog.NewTextHandler(io.Discard, nil)), database.ChannelExecution)
	listener.Reconnect = 10 * time.Millisecond
	key := uuid.NewString()
	wake, cancel := listener.Subscribe(database.ChannelExecution, key)
	defer cancel()
	run(t, listener)
	woken(t, "after LISTEN", wake)

	listening := func() []int32 {
		t.Helper()
		var pids []int32
		if err := pool.QueryRow(t.Context(), `select coalesce(array_agg(pid), '{}') from pg_stat_activity
			where datname = current_database() and pid <> pg_backend_pid() and state = 'idle' and query ilike 'listen%'`).Scan(&pids); err != nil {
			t.Fatal(err)
		}
		return pids
	}
	dropped := listening()
	if _, err := pool.Exec(t.Context(), "select pg_terminate_backend(pid) from unnest($1::int[]) pid", dropped); err != nil {
		t.Fatal(err)
	}
	woken(t, "after the connection dropped", wake)

	for deadline := time.Now().Add(5 * time.Second); ; time.Sleep(5 * time.Millisecond) {
		if pids := listening(); len(pids) == 1 && pids[0] != dropped[0] {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the listener did not connect again")
		}
	}
	select {
	case <-wake:
	default:
	}
	notifyAll(t, pool, database.ChannelExecution, key)
	woken(t, "after a notify on the new connection", wake)
}
