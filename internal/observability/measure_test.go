package observability_test

import (
	"context"
	"log/slog"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/observability"
)

// BenchmarkPublishToThousandSubscribers is the hub's cost of handing one
// event to 1,000 subscribers of its workspace while 1,000 more watch other
// workspaces.
func BenchmarkPublishToThousandSubscribers(b *testing.B) {
	hub := observability.NewChanges(nil, observability.ChangesConfig{Retained: 16384, Buffer: 256, MaxSubscribers: 4000}, nil, slog.New(slog.DiscardHandler))
	ws := identity.WorkspaceID(uuid.New())
	var subs []*observability.Subscription
	for i := range 2000 {
		target := ws
		if i%2 == 1 {
			target = identity.WorkspaceID(uuid.New())
		}
		sub, _, err := hub.Subscribe(target, "test", nil)
		if err != nil {
			b.Fatal(err)
		}
		subs = append(subs, sub)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	for _, sub := range subs {
		go func() {
			for {
				select {
				case <-sub.Events():
				case <-sub.Reset():
					sub.TakeReset()
				case <-ctx.Done():
					return
				}
			}
		}()
	}
	frame := []byte(`id: 1` + "\n" + `event: change` + "\n" + `data: {"seq": 1}` + "\n\n")
	b.ResetTimer()
	for i := range b.N {
		hub.Publish(observability.ChangeEvent{Seq: int64(i), Workspace: ws, Frame: frame})
	}
}

// TestMeasureMetricIngest stores one 5-second tick of 1,000 containers on
// 20 hosts, folds a minute of them and reads one container's hour. Set
// LAZYCLOUD_MEASURE=1 to run it.
func TestMeasureMetricIngest(t *testing.T) {
	if os.Getenv("LAZYCLOUD_MEASURE") == "" {
		t.Skip("set LAZYCLOUD_MEASURE=1 to measure")
	}
	f := newFixture(t, `{}`)
	const hosts, perHost = 20, 50
	type placed struct {
		host      compute.HostID
		container execution.ContainerID
	}
	var all []placed
	for range hosts {
		var host uuid.UUID
		if err := f.pool.QueryRow(t.Context(), `insert into hosts (name, token_hash, state, cpu_millis, memory_bytes)
values ('h', sha256(gen_random_uuid()::text::bytea), 'online', 64000, 1 << 40) returning id`).Scan(&host); err != nil {
			t.Fatal(err)
		}
		rows, err := f.pool.Query(t.Context(), `insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
select $1, $2, 'ready', $3, 1, 1000, 1 << 30, now() from generate_series(1, $4) returning id`, uuid.UUID(f.workspace), f.release, host, perHost)
		if err != nil {
			t.Fatal(err)
		}
		for rows.Next() {
			var id uuid.UUID
			if err := rows.Scan(&id); err != nil {
				t.Fatal(err)
			}
			all = append(all, placed{compute.HostID(host), execution.ContainerID(id)})
		}
		rows.Close()
	}
	minute := time.Now().Truncate(time.Minute).Add(-62 * time.Minute)
	f.exec1("update container_metric_rollup set rolled_through = $1", minute)

	var walBefore, walAfter int64
	wal := func(out *int64) {
		if err := f.pool.QueryRow(t.Context(), "select pg_current_wal_lsn() - '0/0'::pg_lsn").Scan(out); err != nil {
			t.Fatal(err)
		}
	}
	// An hour of ticks, timed per tick.
	var worst, total time.Duration
	const ticks = 720
	wal(&walBefore)
	for tick := range ticks {
		at := minute.Add(time.Duration(tick) * 5 * time.Second)
		batch := make([]observability.MetricSample, len(all))
		for i, p := range all {
			batch[i] = sample(p.host, p.container, at, uint64(1000*i), uint64(64<<20+i))
		}
		began := time.Now()
		stored, err := f.obs.StoreSamples(t.Context(), batch)
		took := time.Since(began)
		if err != nil || stored != int64(len(all)) {
			t.Fatalf("tick %d: %d stored: %v", tick, stored, err)
		}
		total += took
		worst = max(worst, took)
	}
	wal(&walAfter)
	var tableBytes int64
	if err := f.pool.QueryRow(t.Context(), "select pg_total_relation_size('container_metric_samples')").Scan(&tableBytes); err != nil {
		t.Fatal(err)
	}
	t.Logf("store 1,000 samples per tick: mean %s, worst %s over %d ticks; WAL %.0f bytes per sample; an hour of samples takes %.1f MiB",
		total/ticks, worst, ticks, float64(walAfter-walBefore)/float64(ticks*len(all)), float64(tableBytes)/(1<<20))

	var folds []time.Duration
	for {
		began := time.Now()
		result, err := f.obs.RollUp(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		folds = append(folds, time.Since(began))
		if !result.Behind {
			break
		}
	}
	t.Logf("rollup: %d passes of up to ten minutes (10,000 minute points each) took %v", len(folds), folds)

	began := time.Now()
	metrics, err := f.obs.ContainerMetrics(t.Context(), f.workspace, all[0].container, observability.MetricsQuery{Start: &minute})
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("one container's last hour: %d points at %ds in %s", len(metrics.Points), metrics.StepSeconds, time.Since(began))
	step := time.Minute
	began = time.Now()
	week := minute.Add(-7 * 24 * time.Hour)
	metrics, err = f.obs.ContainerMetrics(t.Context(), f.workspace, all[0].container, observability.MetricsQuery{Start: &week, Step: &step})
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("one container's week at the minute step: %d points at %ds in %s", len(metrics.Points), metrics.StepSeconds, time.Since(began))
}
