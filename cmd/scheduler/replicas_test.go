package main

import (
	"bufio"
	"context"
	"log/slog"
	"maps"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// idlePassLimit bounds the passes the leader runs over two of its slow
// ticks while idle: four metered loops run on the slow tick, up to three
// times each as their phases differ, and running the seven quiet loops on
// every tick would add seventy.
const idlePassLimit = 16

// An idle scheduler runs few passes, and a second replica adds none: only
// the elected leader runs timed passes, and with no live work its quiet
// loops wait for a wake. Every pass is at least one transaction, so this
// bounds the idle statement rate without depending on when PostgreSQL
// publishes its statistics.
func TestIdlePassesDoNotGrowWithReplicas(t *testing.T) {
	pool := dbtest.New(t)
	schedulerEnv(t, pool)
	rollupCurrent(t, pool)
	release := holdLeadership(t, pool)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	done := make(chan error, 2)
	var metrics []string
	for range 2 {
		health, scrape := freeAddr(t), freeAddr(t)
		t.Setenv("LAZYCLOUD_HEALTH_ADDR", health)
		t.Setenv("LAZYCLOUD_METRICS_ADDR", scrape)
		go func() { done <- run(ctx, slog.New(slog.DiscardHandler), testTicks()) }()
		// Ready means the replica read its settings and finished a first
		// pass of every loop.
		waitOK(t, "http://"+health+"/readyz")
		metrics = append(metrics, "http://"+scrape+"/metrics")
	}
	release()
	leader, before := led(t, metrics)
	standby := total(passes(t, metrics[1-leader]))
	after := passesUntil(t, metrics[leader], "host_loss", before["host_loss"]+2)
	ran := total(after) - total(before)
	t.Logf("idle passes over two slow ticks: %v by the leader", ran)
	if ran > idlePassLimit {
		t.Errorf("an idle leader ran %v passes over two slow ticks, over %d", ran, idlePassLimit)
	}
	// Only the leader runs timed passes; a wake alone runs one elsewhere.
	if n := total(passes(t, metrics[1-leader])) - standby; n > 1 {
		t.Errorf("the standby ran %v idle passes", n)
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

// rollupCurrent moves the metric rollup's watermark to the present, as an
// idle deployment's is; a watermark behind reruns the rollup pass on every
// replica until it catches up.
func rollupCurrent(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	if _, err := pool.Exec(t.Context(), "update container_metric_rollup set rolled_through = date_trunc('minute', now() - interval '2 minutes')"); err != nil {
		t.Fatal(err)
	}
}

// led waits until a replica behind urls ran the pass every loop runs on
// taking the lead, and returns which and its finished passes by loop.
func led(t *testing.T, urls []string) (int, map[string]float64) {
	t.Helper()
	for deadline := time.Now().Add(15 * time.Second); ; time.Sleep(10 * time.Millisecond) {
		for i, url := range urls {
			counts := passes(t, url)
			if !slices.ContainsFunc(slices.Collect(maps.Values(counts)), func(n float64) bool { return n < 2 }) {
				return i, counts
			}
		}
		if time.Now().After(deadline) {
			t.Fatal("no replica ran every loop's lead pass within 15s")
		}
	}
}

// passesUntil waits until the replica behind url finished n passes of loop
// and returns its finished passes by loop.
func passesUntil(t *testing.T, url, loop string, n float64) map[string]float64 {
	t.Helper()
	for deadline := time.Now().Add(15 * time.Second); ; time.Sleep(10 * time.Millisecond) {
		counts := passes(t, url)
		if counts[loop] >= n {
			return counts
		}
		if time.Now().After(deadline) {
			t.Fatalf("%s finished %v passes within 15s, want %v", loop, counts[loop], n)
		}
	}
}

func total(counts map[string]float64) float64 {
	n := 0.0
	for _, c := range counts {
		n += c
	}
	return n
}

// passes is the finished passes by loop the replica behind url reports.
func passes(t *testing.T, url string) map[string]float64 {
	t.Helper()
	req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	out := map[string]float64{}
	scanner := bufio.NewScanner(resp.Body)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "lazycloud_scheduler_pass_seconds_count{pass=\"") {
			continue
		}
		loop, _, _ := strings.Cut(strings.TrimPrefix(line, "lazycloud_scheduler_pass_seconds_count{pass=\""), "\"")
		fields := strings.Fields(line)
		n, err := strconv.ParseFloat(fields[len(fields)-1], 64)
		if err != nil {
			t.Fatal(err)
		}
		out[loop] = n
	}
	if err := scanner.Err(); err != nil {
		t.Fatal(err)
	}
	return out
}
