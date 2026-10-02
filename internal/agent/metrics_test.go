package agent

import (
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// A running container's use is read from its cgroup and network namespace
// and sent over the session; its report carries how long each start stage
// took; an exited container is no longer sampled.
func TestAgentReportsContainerMetricsAndStartStages(t *testing.T) {
	e := newEnv(t)
	e.metricsInterval = 200 * time.Millisecond
	e.startAgent()
	s := e.session()
	start := e.startCommand("app:handle", 1)
	id := start.GetStart().GetContainerId()
	s.send(t, start)
	report := s.phase(t, id, ready)
	var kinds []hostproto.StartupStageKind
	for _, stage := range report.GetStartup() {
		kinds = append(kinds, stage.GetKind())
		if stage.GetFinishedAt().AsTime().Before(stage.GetStartedAt().AsTime()) {
			t.Fatalf("stage ends before it starts: %v", stage)
		}
	}
	want := []hostproto.StartupStageKind{
		hostproto.StartupStageKind_STARTUP_STAGE_KIND_IMAGE, hostproto.StartupStageKind_STARTUP_STAGE_KIND_SOURCE,
		hostproto.StartupStageKind_STARTUP_STAGE_KIND_CREATE, hostproto.StartupStageKind_STARTUP_STAGE_KIND_RUNTIME,
	}
	if len(kinds) != len(want) {
		t.Fatalf("stages %v", kinds)
	}
	for i := range want {
		if kinds[i] != want[i] {
			t.Fatalf("stages %v", kinds)
		}
	}

	// Printing a lot spends CPU in the container.
	e.completion(e.task(id, `{"args": ["spam", 20000]}`))
	var cpu uint64
	var samples int
	s.until(t, 30*time.Second, func(m *hostproto.HostMessage) bool {
		for _, sample := range m.GetMetrics().GetSamples() {
			if sample.GetContainerId() != id {
				continue
			}
			if sample.GetMemoryRssBytes() < 1<<20 || sample.GetIntervalMs() < 100 || sample.GetIntervalMs() > 5000 {
				t.Fatalf("sample %v", sample)
			}
			cpu += sample.GetCpuUsageUsec()
			samples++
		}
		return samples >= 3 && cpu > 0
	})

	s.send(t, stopCommand(id, 5))
	s.phase(t, id, exited)
	quiet := time.After(time.Second)
	for {
		select {
		case m := <-s.msgs:
			for _, sample := range m.GetMetrics().GetSamples() {
				if sample.GetContainerId() == id {
					t.Fatalf("an exited container was sampled: %v", sample)
				}
			}
			continue
		case <-quiet:
		}
		break
	}
}
