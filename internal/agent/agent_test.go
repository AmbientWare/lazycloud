package agent

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	cerrdefs "github.com/containerd/errdefs"
	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	preparing = hostproto.ContainerPhase_CONTAINER_PHASE_PREPARING
	starting  = hostproto.ContainerPhase_CONTAINER_PHASE_STARTING
	ready     = hostproto.ContainerPhase_CONTAINER_PHASE_READY
	exited    = hostproto.ContainerPhase_CONTAINER_PHASE_EXITED
)

func acked(id string) func(*hostproto.HostMessage) bool {
	return func(m *hostproto.HostMessage) bool { return m.GetAck().GetCommandId() == id }
}

func result(t *testing.T, c *hostproto.CompleteTaskRequest) string {
	t.Helper()
	success := c.GetSuccess()
	if success == nil || success.GetEncoding() != hostproto.PayloadEncoding_PAYLOAD_ENCODING_JSON {
		t.Fatalf("expected a JSON success, got %v", c)
	}
	return string(success.GetResult())
}

// startReady starts a container and waits until it is ready, logging how
// long each stage took.
func (e *env) startReady(s *serverSession, slots int32) string {
	e.t.Helper()
	start := e.startCommand("app:handle", slots)
	id := start.GetStart().GetContainerId()
	began := time.Now()
	s.send(e.t, start)
	// The agent reports PREPARING as it accepts the command, before the ack.
	s.phase(e.t, id, preparing)
	s.until(e.t, 10*time.Second, acked(start.GetCommandId()))
	s.phase(e.t, id, starting)
	startedAt := time.Now()
	s.phase(e.t, id, ready)
	e.t.Logf("start command to STARTING (image, source download and extract, docker create and start): %s; STARTING to READY (supervisor and %d runners loaded): %s",
		startedAt.Sub(began), slots, time.Since(startedAt))
	return id
}

func TestAgentRunsTasksThenStops(t *testing.T) {
	e := newEnv(t)
	e.startAgent()
	s := e.session()
	if len(s.hello.GetContainers()) != 0 || s.hello.GetCapacity().GetCpuMillis() != 4000 {
		t.Fatalf("hello %v", s.hello)
	}
	id := e.startReady(s, 2)

	claimed := time.Now()
	attempt := e.task(id, `{"args": ["total", [1200, 3500, 800]]}`)
	if got := result(t, e.completion(attempt)); got != "5500" {
		t.Fatalf("result %q", got)
	}
	t.Logf("queued task to completion on a ready container: %s", time.Since(claimed))
	// Logs are flushed before the completion, so they are already stored.
	if out := e.server.output(attempt); !strings.Contains(out, "summing 3 values") || !strings.Contains(out, "stderr line") {
		t.Fatalf("attempt output %q", out)
	}

	failed := e.task(id, `{"args": ["nope"]}`)
	failure := e.completion(failed).GetFailure()
	if failure.GetKind() != hostproto.AttemptFailureKind_ATTEMPT_FAILURE_KIND_USER_ERROR || failure.GetError().GetType() != "ValueError" {
		t.Fatalf("failure %v", failure)
	}

	// The supervisor is PID 1 and reaps re-parented processes.
	e.completion(e.task(id, `{"args": ["orphan"]}`))
	time.Sleep(time.Second)
	if got := result(t, e.completion(e.task(id, `{"args": ["zombies"]}`))); got != "0" {
		t.Fatalf("%s zombie processes in the container", got)
	}

	stop := stopCommand(id, 10)
	s.send(t, stop)
	report := s.phase(t, id, exited)
	if report.GetExit().GetReason() != hostproto.ExitReason_EXIT_REASON_STOPPED {
		t.Fatalf("exit %v", report.GetExit())
	}
	e.eventually("container and its directory are removed", func() bool {
		_, err := os.Stat(filepath.Join(e.stateDir, "containers", id))
		return e.containers() == 0 && os.IsNotExist(err)
	})
	// A repeated start of an exited container restates the exit.
	s.send(t, &hostproto.ServerMessage{CommandId: "again", Body: e.startCommand("app:handle", 2).GetBody()})
	s.until(t, 10*time.Second, acked("again"))
}

func TestAgentCancelsAndRecoversSlots(t *testing.T) {
	e := newEnv(t)
	e.startAgent()
	s := e.session()
	id := e.startReady(s, 1)

	hung := e.task(id, `{"args": ["hang"]}`)
	e.eventually("hung attempt logs its output", func() bool { return strings.Contains(e.server.output(hung), "hanging") })
	cancelled := time.Now()
	s.send(t, cancelCommand(id, hung))
	next := e.task(id, `{"args": ["total", [1, 2]]}`)
	// completion fails the test on any other attempt, so the cancelled one
	// reports nothing.
	if got := result(t, e.completion(next)); got != "3" {
		t.Fatalf("after cancel %q", got)
	}
	t.Logf("cancel to next completion on the restarted slot: %s", time.Since(cancelled))

	crashed := e.task(id, `{"args": ["crash"]}`)
	failure := e.completion(crashed).GetFailure()
	if failure.GetKind() != hostproto.AttemptFailureKind_ATTEMPT_FAILURE_KIND_CRASHED {
		t.Fatalf("crash %v", failure)
	}
	if got := result(t, e.completion(e.task(id, `{"args": ["total", [5]]}`))); got != "5" {
		t.Fatalf("after crash %q", got)
	}
}

// A slow log consumer holds the runner back instead of losing output, and
// the result still arrives after all of it.
func TestAgentAppliesLogBackpressure(t *testing.T) {
	const lines = 8192 // 8 MiB, past every buffer between runner and server
	e := newEnv(t)
	e.server.appendDelay = 10 * time.Millisecond
	e.startAgent()
	s := e.session()
	id := e.startReady(s, 1)

	began := time.Now()
	attempt := e.task(id, fmt.Sprintf(`{"args": ["spam", %d]}`, lines))
	if got := result(t, e.completion(attempt)); got != strconv.Itoa(lines) {
		t.Fatalf("result %q", got)
	}
	output := strings.Split(strings.TrimSuffix(e.server.output(attempt), "\n"), "\n")
	if len(output) != lines {
		t.Fatalf("got %d of %d lines", len(output), lines)
	}
	for i, line := range output {
		if !strings.HasPrefix(line, fmt.Sprintf("%08d", i)) || len(line) != 1023 {
			t.Fatalf("line %d is %.20q (%d bytes)", i, line, len(line))
		}
	}
	t.Logf("8 MiB of output through a 10 ms-per-batch consumer: %s", time.Since(began))
}

func TestAgentReportsLoadAndStartFailures(t *testing.T) {
	e := newEnv(t)
	e.startAgent()
	s := e.session()

	broken := e.startCommand("broken:handle", 2)
	s.send(t, broken)
	report := s.phase(t, broken.GetStart().GetContainerId(), exited)
	exit := report.GetExit()
	if exit.GetReason() != hostproto.ExitReason_EXIT_REASON_LOAD_ERROR || exit.GetError().GetType() != "RuntimeError" ||
		!strings.Contains(exit.GetError().GetTraceback(), "broken.py") {
		t.Fatalf("load error exit %v", exit)
	}

	corrupt := e.startCommand("app:handle", 1)
	corrupt.GetStart().Source = &hostproto.Source{Sha256: strings.Repeat("0", 64), Url: e.source.GetUrl()}
	s.send(t, corrupt)
	exit = s.phase(t, corrupt.GetStart().GetContainerId(), exited).GetExit()
	if exit.GetReason() != hostproto.ExitReason_EXIT_REASON_START_FAILED || !strings.Contains(exit.GetMessage(), "digest mismatch") {
		t.Fatalf("start failure exit %v", exit)
	}
	e.eventually("failed containers are removed", func() bool { return e.containers() == 0 })
}

func TestAgentAdoptsRunningContainersAfterRestart(t *testing.T) {
	e := newEnv(t)
	first := e.startAgent()
	s := e.session()
	id := e.startReady(s, 2)
	attempt := e.task(id, `{"args": ["sleep", 3]}`)
	e.eventually("task is claimed", func() bool {
		e.server.mu.Lock()
		defer e.server.mu.Unlock()
		return len(e.server.queued[id]) == 0
	})
	time.Sleep(300 * time.Millisecond)
	first.stop()

	restarted := time.Now()
	e.startAgent()
	s = e.session()
	if containers := s.hello.GetContainers(); len(containers) != 1 || containers[0].GetContainerId() != id || containers[0].GetPhase() == exited {
		t.Fatalf("hello after restart %v", containers)
	}
	report := s.phase(t, id, ready)
	if running := report.GetRunningAttempts(); len(running) != 1 || running[0] != attempt {
		t.Fatalf("running attempts after restart %v", running)
	}
	t.Logf("agent restart to adopted container ready: %s", time.Since(restarted))
	if got := result(t, e.completion(attempt)); got != `"slept"` {
		t.Fatalf("attempt that outlived the agent %q", got)
	}
	if got := result(t, e.completion(e.task(id, `{"args": ["total", [2, 2]]}`))); got != "4" {
		t.Fatalf("after adoption %q", got)
	}
}

func TestAgentReconnectsAfterServerRestart(t *testing.T) {
	e := newEnv(t)
	e.startAgent()
	s := e.session()
	id := e.startReady(s, 1)

	e.grpc.Stop()
	stopped := time.Now()
	e.grpc, _ = e.server.serve(t, e.address)
	s = e.session()
	t.Logf("server restart to new session: %s", time.Since(stopped))
	if containers := s.hello.GetContainers(); len(containers) != 1 || containers[0].GetPhase() != ready {
		t.Fatalf("hello after reconnect %v", containers)
	}
	if got := result(t, e.completion(e.task(id, `{"args": ["total", [7]]}`))); got != "7" {
		t.Fatalf("after reconnect %q", got)
	}
}

func TestAgentPullsAMissingImage(t *testing.T) {
	const image = "python:3.12-alpine"
	e := newEnv(t)
	if _, err := e.docker.ImageInspect(t.Context(), image); err == nil {
		t.Skipf("%s is already present, so there is nothing to pull", image)
	} else if !cerrdefs.IsNotFound(err) {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		e.removeContainers()
		if _, err := e.docker.ImageRemove(context.Background(), image, client.ImageRemoveOptions{}); err != nil {
			t.Errorf("remove pulled image: %v", err)
		}
	})
	e.startAgent()
	s := e.session()
	start := e.startCommand("app:handle", 1)
	start.GetStart().Image = image
	began := time.Now()
	s.send(t, start)
	s.phase(t, start.GetStart().GetContainerId(), starting)
	t.Logf("start command to STARTING with an image pull: %s", time.Since(began))
	s.phase(t, start.GetStart().GetContainerId(), ready)
}

func (e *env) eventually(what string, ok func() bool) {
	e.t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for !ok() {
		if time.Now().After(deadline) {
			e.t.Fatalf("timed out waiting until %s", what)
		}
		time.Sleep(50 * time.Millisecond)
	}
}
