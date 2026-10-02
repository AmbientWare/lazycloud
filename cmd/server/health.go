package main

import (
	"fmt"
	"net/http"
	"sync/atomic"
)

// health answers the orchestrator's probes on their own listener, apart
// from the public API. /healthz reports that the process serves; /readyz
// turns false once shutdown starts, so load balancers stop routing new
// requests here before the listeners close. Readiness leaves the database
// out: when it is down every replica would turn unready together, and
// clients would lose the typed errors the API returns for it.
type health struct {
	draining atomic.Bool
}

func (h *health) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeProbe(w, http.StatusOK, "ok")
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		if h.draining.Load() {
			writeProbe(w, http.StatusServiceUnavailable, "draining")
			return
		}
		writeProbe(w, http.StatusOK, "ok")
	})
	return mux
}

func writeProbe(w http.ResponseWriter, status int, body string) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_, _ = fmt.Fprintln(w, body)
}
