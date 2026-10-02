package agent

import (
	"strings"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// agentMetrics are the agent's own Prometheus collectors.
type agentMetrics struct {
	sessions prometheus.Counter
	sampling prometheus.Histogram
	samples  prometheus.Counter
}

func newAgentMetrics(a *Agent, registerer prometheus.Registerer) agentMetrics {
	m := agentMetrics{
		sessions: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "lazycloud_agent_sessions_total", Help: "Control sessions the agent opened.",
		}),
		sampling: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name: "lazycloud_agent_metric_sampling_seconds", Help: "Time to sample every running container once.",
			Buckets: prometheus.ExponentialBuckets(0.0001, 3, 12),
		}),
		samples: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "lazycloud_agent_metric_samples_total", Help: "Container metric samples sent.",
		}),
	}
	if registerer != nil {
		registerer.MustRegister(m.sessions, m.sampling, m.samples, containerCollector{a})
	}
	return m
}

var containersDesc = prometheus.NewDesc("lazycloud_agent_containers", "Containers the agent tracks by phase.", []string{"phase"}, nil) //nolint:gochecknoglobals // An immutable metric description.

// containerCollector counts containers by phase when /metrics is scraped.
type containerCollector struct{ a *Agent }

func (containerCollector) Describe(ch chan<- *prometheus.Desc) { ch <- containersDesc }

func (c containerCollector) Collect(ch chan<- prometheus.Metric) {
	c.a.mu.Lock()
	list := make([]*container, 0, len(c.a.containers))
	for _, ctr := range c.a.containers {
		list = append(list, ctr)
	}
	c.a.mu.Unlock()
	counts := map[hostproto.ContainerPhase]int{}
	for _, ctr := range list {
		ctr.mu.Lock()
		counts[ctr.phase]++
		ctr.mu.Unlock()
	}
	for phase, n := range counts {
		name := strings.ToLower(strings.TrimPrefix(phase.String(), "CONTAINER_PHASE_"))
		ch <- prometheus.MustNewConstMetric(containersDesc, prometheus.GaugeValue, float64(n), name)
	}
}
