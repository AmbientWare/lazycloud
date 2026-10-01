package main

import (
	"context"
	"fmt"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// readinessTimeout bounds the database check of one readiness probe.
const readinessTimeout = 2 * time.Second

// health answers the orchestrator's probes. /healthz reports that the
// process serves requests; /readyz also requires the database and turns
// false once shutdown starts, so load balancers stop routing new requests
// here before the listeners close.
type health struct {
	pool     *pgxpool.Pool
	draining atomic.Bool
}

// withProbes serves the probes beside next.
func (h *health) withProbes(next http.Handler) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeProbe(w, http.StatusOK, "ok")
	})
	mux.HandleFunc("GET /readyz", h.ready)
	mux.Handle("/", next)
	return mux
}

func (h *health) ready(w http.ResponseWriter, r *http.Request) {
	if h.draining.Load() {
		writeProbe(w, http.StatusServiceUnavailable, "draining")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), readinessTimeout)
	defer cancel()
	if err := h.pool.Ping(ctx); err != nil {
		writeProbe(w, http.StatusServiceUnavailable, "database unavailable")
		return
	}
	writeProbe(w, http.StatusOK, "ok")
}

func writeProbe(w http.ResponseWriter, status int, body string) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_, _ = fmt.Fprintln(w, body)
}
