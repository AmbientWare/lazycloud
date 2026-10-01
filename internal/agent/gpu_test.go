package agent

import (
	"os/exec"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

func (e *env) gpuStart(gpus int32) *hostproto.ServerMessage {
	start := e.startCommand("app:handle", 1)
	start.GetStart().Resources.GpuCount = gpus
	return start
}

func TestAgentGivesContainersFreeGPUs(t *testing.T) {
	devices := detectGPUs(t.Context())
	if len(devices) == 0 || !nvidiaRuntimeInstalled() {
		t.Skip("needs an NVIDIA GPU and the NVIDIA container runtime")
	}
	e := newEnv(t)
	all := e.startAgent(func(cfg *Config) { cfg.Limits.GPUs = new(len(devices)) })
	s := e.session()

	first := e.gpuStart(int32(len(devices)))
	firstID := first.GetStart().GetContainerId()
	s.send(t, first)
	s.phase(t, firstID, ready)
	inspect, err := e.docker.ContainerInspect(t.Context(), "lazycloud-"+firstID, client.ContainerInspectOptions{})
	if err != nil {
		t.Fatal(err)
	}
	requests := inspect.Container.HostConfig.DeviceRequests
	if len(requests) != 1 || requests[0].Driver != "nvidia" || len(requests[0].DeviceIDs) != len(devices) || !slices.Contains(requests[0].DeviceIDs, devices[0].UUID) {
		t.Fatalf("device requests %+v", requests)
	}
	out, err := exec.CommandContext(t.Context(), "docker", "exec", "lazycloud-"+firstID, "nvidia-smi", "-L").CombinedOutput()
	if err != nil || !strings.Contains(string(out), devices[0].UUID) {
		t.Fatalf("the container does not see its GPU: %v\n%s", err, out)
	}

	busy := func(s *serverSession) {
		t.Helper()
		start := e.gpuStart(1)
		s.send(t, start)
		report := s.phase(t, start.GetStart().GetContainerId(), exited)
		if report.GetExit().GetReason() != hostproto.ExitReason_EXIT_REASON_START_FAILED || !strings.Contains(report.GetExit().GetMessage(), "GPUs") {
			t.Fatalf("a start with no free GPU exited with %v", report.GetExit())
		}
	}
	busy(s)

	// An adopted container keeps its devices.
	all.stop()
	e.startAgent(func(cfg *Config) { cfg.Limits.GPUs = new(len(devices)) })
	s = e.session()
	busy(s)

	began := time.Now()
	s.send(t, stopCommand(firstID, 1))
	s.phase(t, firstID, exited)
	again := e.gpuStart(1)
	s.send(t, again)
	s.phase(t, again.GetStart().GetContainerId(), ready)
	t.Logf("GPU freed and reassigned in %s", time.Since(began))
}
