package main

import (
	"context"
	"fmt"
	"net/http"
	"slices"
	"strings"
	"sync"
	"time"
)

// stallAllowance is how much longer than its interval a loop may go without
// finishing a pass before /healthz reports it stalled.
const stallAllowance = 2 * time.Minute

// heartbeats records when each scheduler loop last finished a pass. The
// health endpoints read it: /readyz once every loop has finished a pass,
// /healthz while none has stalled. A loop wedged on a hung connection or
// lock then restarts the process instead of silently stopping its work.
type heartbeats struct {
	mu    sync.Mutex
	loops map[string]*heartbeat
}

type heartbeat struct {
	limit time.Duration
	// since is when the loop was registered or last finished a pass.
	since    time.Time
	finished bool
}

func newHeartbeats() *heartbeats {
	return &heartbeats{loops: map[string]*heartbeat{}}
}

// track registers the loop name, run every interval, and returns pass
// wrapped to record each finished pass.
func (h *heartbeats) track(name string, interval time.Duration, pass func(context.Context) bool) func(context.Context) bool {
	h.mu.Lock()
	h.loops[name] = &heartbeat{limit: interval + stallAllowance, since: time.Now()}
	h.mu.Unlock()
	return func(ctx context.Context) bool {
		skipped := pass(ctx)
		h.mu.Lock()
		beat := h.loops[name]
		beat.since, beat.finished = time.Now(), true
		h.mu.Unlock()
		return skipped
	}
}

// check returns the loops that have not finished a first pass and those
// that stalled at now.
func (h *heartbeats) check(now time.Time) (starting, stalled []string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for name, beat := range h.loops {
		if now.Sub(beat.since) > beat.limit {
			stalled = append(stalled, name)
		} else if !beat.finished {
			starting = append(starting, name)
		}
	}
	slices.Sort(starting)
	slices.Sort(stalled)
	return starting, stalled
}

// handler serves /healthz and /readyz.
func (h *heartbeats) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		if _, stalled := h.check(time.Now()); len(stalled) > 0 {
			writeProbe(w, http.StatusServiceUnavailable, "stalled: "+strings.Join(stalled, ", "))
			return
		}
		writeProbe(w, http.StatusOK, "ok")
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		starting, stalled := h.check(time.Now())
		switch {
		case len(stalled) > 0:
			writeProbe(w, http.StatusServiceUnavailable, "stalled: "+strings.Join(stalled, ", "))
		case len(starting) > 0:
			writeProbe(w, http.StatusServiceUnavailable, "starting: "+strings.Join(starting, ", "))
		default:
			writeProbe(w, http.StatusOK, "ok")
		}
	})
	return mux
}

func writeProbe(w http.ResponseWriter, status int, body string) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_, _ = fmt.Fprintln(w, body)
}
