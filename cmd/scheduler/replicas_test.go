package main

import (
	"bufio"
	"context"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

const (
	// window is how long idle passes are counted: a cycle of the 5-second
	// loops, so a replica that runs timed passes runs some.
	window = 6 * time.Second
	// idlePassLimit bounds the idle passes a second across the deployment;
	// running each quiet loop every second makes over six.
	idlePassLimit = 1.5
	// quietPoll is how long the replicas must run no pass for their start-up
	// and leadership passes to count as done.
	quietPoll = 300 * time.Millisecond
)

// An idle scheduler runs few passes, and a second replica adds none: only
// the elected leader runs timed passes, and with no live work its quiet
// loops wait for a wake. Every pass is at least one transaction, so this
// bounds the idle statement rate without depending on when PostgreSQL
// publishes its statistics.
func TestIdlePassesDoNotGrowWithReplicas(t *testing.T) {
	pool := dbtest.New(t)
	schedulerEnv(t, pool)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	done := make(chan error, 2)
	var metrics []string
	for range 2 {
		health, scrape := freeAddr(t), freeAddr(t)
		t.Setenv("LAZYCLOUD_HEALTH_ADDR", health)
		t.Setenv("LAZYCLOUD_METRICS_ADDR", scrape)
		go func() { done <- run(ctx, slog.New(slog.DiscardHandler)) }()
		// Ready means the replica read its settings and finished a first
		// pass of every loop.
		waitOK(t, "http://"+health+"/readyz")
		metrics = append(metrics, "http://"+scrape+"/metrics")
	}
	before := settled(t, pool, metrics)
	time.Sleep(window)
	after := passes(t, metrics)
	ran := [2]float64{after[0] - before[0], after[1] - before[1]}
	t.Logf("idle passes in %s: %v by replica", window, ran)
	if rate := (ran[0] + ran[1]) / window.Seconds(); rate > idlePassLimit {
		t.Errorf("two idle replicas run %.2f passes a second, over %.1f", rate, idlePassLimit)
	}
	// Only the leader runs timed passes; a wake alone runs one elsewhere.
	if min(ran[0], ran[1]) > 1 {
		t.Errorf("both replicas ran idle passes, %v in %s", ran, window)
	}

	cancel()
	for range 2 {
		select {
		case err := <-done:
			if err != nil {
				t.Fatalf("run: %v", err)
			}
		case <-time.After(10 * time.Second):
			t.Fatal("a scheduler did not stop within 10s of shutdown")
		}
	}
}

// settled waits until a replica leads and the replicas behind urls run no
// pass for quietPoll: a replica runs every loop when it takes the lead and
// again when it finds no live work. It returns their finished passes.
func settled(t *testing.T, pool *pgxpool.Pool, urls []string) [2]float64 {
	t.Helper()
	for led := false; !led; time.Sleep(10 * time.Millisecond) {
		if err := pool.QueryRow(t.Context(), "select exists (select 1 from pg_locks where locktype = 'advisory' and granted)").Scan(&led); err != nil {
			t.Fatal(err)
		}
	}
	for before := passes(t, urls); ; {
		time.Sleep(quietPoll)
		now := passes(t, urls)
		if now == before {
			return now
		}
		before = now
	}
}

// passes is the finished passes each replica behind urls reports.
func passes(t *testing.T, urls []string) [2]float64 {
	t.Helper()
	var out [2]float64
	for i, url := range urls {
		req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, url, nil)
		if err != nil {
			t.Fatal(err)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		scanner := bufio.NewScanner(resp.Body)
		for scanner.Scan() {
			line := scanner.Text()
			if !strings.HasPrefix(line, "lazycloud_scheduler_pass_seconds_count") {
				continue
			}
			fields := strings.Fields(line)
			n, err := strconv.ParseFloat(fields[len(fields)-1], 64)
			if err != nil {
				t.Fatal(err)
			}
			out[i] += n
		}
		_ = resp.Body.Close()
		if err := scanner.Err(); err != nil {
			t.Fatal(err)
		}
	}
	return out
}
