package telemetry

import (
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
)

// RegisterPool exports the database pool's connections and waits, read when
// /metrics is scraped.
func (t *Telemetry) RegisterPool(pool *pgxpool.Pool) {
	gauge := func(name, help string, read func(*pgxpool.Stat) float64) prometheus.Collector {
		return prometheus.NewGaugeFunc(prometheus.GaugeOpts{Name: name, Help: help}, func() float64 { return read(pool.Stat()) })
	}
	counter := func(name, help string, read func(*pgxpool.Stat) float64) prometheus.Collector {
		return prometheus.NewCounterFunc(prometheus.CounterOpts{Name: name, Help: help}, func() float64 { return read(pool.Stat()) })
	}
	t.Registry.MustRegister(
		gauge("lazycloud_db_connections_acquired", "Pool connections in use.",
			func(s *pgxpool.Stat) float64 { return float64(s.AcquiredConns()) }),
		gauge("lazycloud_db_connections_idle", "Idle pool connections.",
			func(s *pgxpool.Stat) float64 { return float64(s.IdleConns()) }),
		gauge("lazycloud_db_connections_max", "The pool's connection limit.",
			func(s *pgxpool.Stat) float64 { return float64(s.MaxConns()) }),
		counter("lazycloud_db_acquire_waits_total", "Acquires that waited for a connection.",
			func(s *pgxpool.Stat) float64 { return float64(s.EmptyAcquireCount()) }),
		counter("lazycloud_db_acquire_wait_seconds_total", "Time acquires spent waiting for a connection.",
			func(s *pgxpool.Stat) float64 { return s.EmptyAcquireWaitTime().Seconds() }),
	)
}
