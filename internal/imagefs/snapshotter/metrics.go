package snapshotter

import (
	"fmt"

	"github.com/prometheus/client_golang/prometheus"
)

// metrics count the snapshotter's reads from the store and its cache.
type metrics struct {
	readFailures  prometheus.Counter
	framesFetched prometheus.Counter
	bytesFetched  prometheus.Counter
	cacheHits     prometheus.Counter
	evictions     prometheus.Counter
	storeFailures prometheus.Counter
	cacheBytes    prometheus.Gauge
	mountedLayers prometheus.Gauge
}

func newMetrics(registry prometheus.Registerer) (*metrics, error) {
	counter := func(name, help string) prometheus.Counter {
		return prometheus.NewCounter(prometheus.CounterOpts{Namespace: "lazycloud", Subsystem: "snapshotter", Name: name, Help: help})
	}
	gauge := func(name, help string) prometheus.Gauge {
		return prometheus.NewGauge(prometheus.GaugeOpts{Namespace: "lazycloud", Subsystem: "snapshotter", Name: name, Help: help})
	}
	m := &metrics{
		readFailures:  counter("read_failures_total", "Container reads of a lazy layer answered with EIO."),
		framesFetched: counter("frames_fetched_total", "Frames read from the layer store."),
		bytesFetched:  counter("fetched_bytes_total", "Compressed bytes read from the layer store."),
		cacheHits:     counter("cache_hits_total", "Frame reads served from the local cache."),
		evictions:     counter("cache_evictions_total", "Frames evicted from the local cache."),
		storeFailures: counter("cache_write_failures_total", "Fetched frames the local cache failed to keep."),
		cacheBytes:    gauge("cache_bytes", "Bytes of frames in the local cache."),
		mountedLayers: gauge("mounted_layers", "Lazy layers mounted."),
	}
	for _, c := range []prometheus.Collector{m.readFailures, m.framesFetched, m.bytesFetched, m.cacheHits, m.evictions, m.storeFailures, m.cacheBytes, m.mountedLayers} {
		if err := registry.Register(c); err != nil {
			return nil, fmt.Errorf("register snapshotter metrics: %w", err)
		}
	}
	return m, nil
}
