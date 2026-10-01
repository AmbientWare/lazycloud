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

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

const (
	// settle lets a replica's start-up passes and first wakes finish.
	settle = 2 * time.Second
	// window is how long idle passes are counted.
	window = 6 * time.Second
	// idlePassLimit bounds idle passes a second across the deployment; a
	// scheduler that ran each of its loops every second made over six.
	idlePassLimit = 1.5
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
	start := func() {
		health, scrape := freeAddr(t), freeAddr(t)
		t.Setenv("LAZYCLOUD_HEALTH_ADDR", health)
		t.Setenv("LAZYCLOUD_METRICS_ADDR", scrape)
		go func() { done <- run(ctx, slog.New(slog.DiscardHandler)) }()
		// Ready means the replica read its settings and finished a first
		// pass of every loop.
		waitStatus(t, "http://"+health+"/readyz", http.StatusOK)
		metrics = append(metrics, "http://"+scrape+"/metrics")
		time.Sleep(settle)
	}
	rate := func() float64 {
		before := passes(t, metrics)
		time.Sleep(window)
		return (passes(t, metrics) - before) / window.Seconds()
	}

	start()
	one := rate()
	start()
	two := rate()
	t.Logf("idle passes a second: %.2f with one replica, %.2f with two", one, two)
	if one > idlePassLimit {
		t.Errorf("one idle replica runs %.2f passes a second, over %.1f", one, idlePassLimit)
	}
	// The 5-second host loss loop lands in a window once or twice.
	if two > one+0.5 {
		t.Errorf("a second replica raised idle passes from %.2f to %.2f a second", one, two)
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

// passes sums the finished passes the replicas behind urls report.
func passes(t *testing.T, urls []string) float64 {
	t.Helper()
	total := 0.0
	for _, url := range urls {
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
			total += n
		}
		_ = resp.Body.Close()
		if err := scanner.Err(); err != nil {
			t.Fatal(err)
		}
	}
	return total
}
