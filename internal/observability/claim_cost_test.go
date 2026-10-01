package observability_test

import (
	"context"
	"log/slog"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
)

// TestMeasureClaimThroughput claims 10,000 queued tasks with 16 hosts of 8
// claimers each, one task per claim, with the task change trigger on and
// off. Set LAZYCLOUD_MEASURE=1 to run it.
func TestMeasureClaimThroughput(t *testing.T) {
	if os.Getenv("LAZYCLOUD_MEASURE") == "" {
		t.Skip("set LAZYCLOUD_MEASURE=1 to measure")
	}
	const hosts, claimers, tasks = 16, 8, 10000
	f := newFixture(t, `{"max_pending_tasks": 1000000}`)
	config := f.pool.Config()
	config.MaxConns = hosts*claimers + 8
	pool, err := pgxpool.NewWithConfig(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	exec := execution.NewExecution(pool)
	type slot struct {
		host      compute.HostID
		container execution.ContainerID
	}
	var slots []slot
	for range hosts {
		var host uuid.UUID
		if err := f.pool.QueryRow(t.Context(), `insert into hosts (name, token_hash, state, cpu_millis, memory_bytes)
values ('h', sha256(gen_random_uuid()::text::bytea), 'online', 64000, 1 << 40) returning id`).Scan(&host); err != nil {
			t.Fatal(err)
		}
		for range claimers {
			var container uuid.UUID
			if err := f.pool.QueryRow(t.Context(), `insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at)
values ($1, $2, 'ready', $3, 100000, 1000, 1 << 30, now(), now()) returning id`, uuid.UUID(f.workspace), f.release, host).Scan(&container); err != nil {
				t.Fatal(err)
			}
			slots = append(slots, slot{compute.HostID(host), execution.ContainerID(container)})
		}
	}
	listener := database.NewListener(pool, slog.New(slog.DiscardHandler), database.ChannelClaim)
	ctx, stop := context.WithCancel(t.Context())
	defer stop()
	go func() { _ = f.obs.RunStartedPublisher(ctx) }()
	run := func(label string) {
		f.exec1("delete from tasks")
		for range tasks / 2000 {
			f.submit(2000)
		}
		var claimed atomic.Int64
		var wg sync.WaitGroup
		began := time.Now()
		for _, s := range slots {
			wg.Go(func() {
				for {
					got, err := exec.ClaimTasks(t.Context(), listener, s.host, s.container, 1, 0)
					if err != nil {
						t.Error(err)
						return
					}
					if len(got) == 0 {
						return
					}
					claimed.Add(int64(len(got)))
					started := make([]execution.TaskID, len(got))
					for n, c := range got {
						started[n] = c.Task
					}
					f.obs.TasksStarted(started)
				}
			})
		}
		wg.Wait()
		took := time.Since(began)
		t.Logf("%s: %d claims by %d claimers in %s, %.0f claims/s", label, claimed.Load(), len(slots), took, float64(claimed.Load())/took.Seconds())
	}
	for range 3 {
		run("trigger on, started tasks published coalesced")
		f.exec1("alter table tasks disable trigger tasks_updated")
		run("trigger off")
		f.exec1("alter table tasks enable trigger tasks_updated")
	}
}
