package main

import (
	"bytes"
	"fmt"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
)

// A call on a host already serving waits for no capacity; one whose host
// resumed after its container was created waits until the host is ready,
// and one never placed waited for capacity to the end.
func TestSyntheticLatencySplitsCapacityWaitFromPlacement(t *testing.T) {
	at := func(ms int) *time.Time {
		v := time.Date(2026, 10, 3, 0, 0, 0, 0, time.UTC).Add(time.Duration(ms) * time.Millisecond)
		return &v
	}
	for name, c := range map[string]struct {
		latency                         execution.TaskLatency
		admission, capacity, run, total time.Duration
	}{
		"warm host": {
			execution.TaskLatency{Submitted: *at(0), ContainerCreated: at(200), HostReady: at(-60_000), Placed: at(500), Finished: at(1500)},
			500 * time.Millisecond, 0, time.Second, 1500 * time.Millisecond,
		},
		"resumed reserve": {
			execution.TaskLatency{Submitted: *at(0), ContainerCreated: at(200), HostReady: at(12_000), Placed: at(12_500), Finished: at(14_000)},
			700 * time.Millisecond, 11_800 * time.Millisecond, 1500 * time.Millisecond, 14 * time.Second,
		},
		"never placed": {
			execution.TaskLatency{Submitted: *at(0), ContainerCreated: at(200), Finished: at(900_000)},
			200 * time.Millisecond, 899_800 * time.Millisecond, 0, 900 * time.Second,
		},
	} {
		got := splitLatency(c.latency)
		if got.admission != c.admission || got.capacity != c.capacity || got.execution != c.run || got.total != c.total {
			t.Errorf("%s: %+v", name, got)
		}
	}
}

// Until the probe app is deployed the check submits nothing and logs no
// error: one line says the app is missing, and later passes stay quiet.
func TestSyntheticCheckStaysQuietWhileTheAppIsNotDeployed(t *testing.T) {
	pool := dbtest.New(t)
	var logs bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	check, err := newSyntheticCheck(fmt.Sprintf("%s/synthetic", uuid.New()), execution.NewExecution(pool), func() bool { return true }, logger)
	if err != nil {
		t.Fatal(err)
	}
	for range 3 {
		check.pass(t.Context())
	}
	var tasks int
	if err := pool.QueryRow(t.Context(), "select count(*) from tasks").Scan(&tasks); err != nil || tasks != 0 || len(check.round) != 0 {
		t.Fatalf("submitted %d tasks, round %v: %v", tasks, check.round, err)
	}
	if strings.Contains(logs.String(), "level=ERROR") || strings.Count(logs.String(), "synthetic app not deployed") != 1 {
		t.Fatalf("logs:\n%s", logs.String())
	}
}
