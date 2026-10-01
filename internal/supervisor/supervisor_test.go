package supervisor

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// linkServer is the agent's side of ContainerLink, driven by the test.
type linkServer struct {
	hostproto.UnimplementedContainerLinkServer
	conns chan *linkConn
}

type linkConn struct {
	stream hostproto.ContainerLink_ConnectServer
	msgs   chan *hostproto.SupervisorMessage
	end    chan struct{}
}

func (l *linkServer) Connect(stream hostproto.ContainerLink_ConnectServer) error {
	c := &linkConn{stream: stream, msgs: make(chan *hostproto.SupervisorMessage, 1024), end: make(chan struct{})}
	l.conns <- c
	received := make(chan struct{})
	go func() {
		defer close(received)
		defer close(c.msgs)
		for {
			m, err := stream.Recv()
			if err != nil {
				return
			}
			c.msgs <- m
		}
	}()
	select {
	case <-c.end:
		return errors.New("link ended by test")
	case <-received:
		return nil
	}
}

type harness struct {
	t      *testing.T
	socket string
	server *linkServer
	grpc   *grpc.Server
	result chan error
	cancel context.CancelFunc
}

// startSupervisor runs a supervisor against a test link server. The runner
// is testdata's protocol runner and the handler module lives in
// testdata/workspace.
func startSupervisor(t *testing.T) *harness {
	t.Helper()
	runtime, err := filepath.Abs("testdata/runtime/3.12")
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("PYTHONPATH", runtime)
	dir, err := os.MkdirTemp("", "lcsup")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	ctx, cancel := context.WithCancel(context.Background())
	h := &harness{
		t: t, socket: filepath.Join(dir, "agent.sock"), server: &linkServer{conns: make(chan *linkConn, 4)},
		result: make(chan error, 1), cancel: cancel,
	}
	h.listen()
	go func() {
		h.result <- Run(ctx, Config{Socket: h.socket, Logger: slog.New(slog.NewTextHandler(os.Stderr, nil))})
	}()
	t.Cleanup(func() {
		cancel()
		select {
		case <-h.result:
		case <-time.After(10 * time.Second):
			t.Error("supervisor did not stop")
		}
		h.grpc.Stop()
	})
	return h
}

// listen serves the link socket, as an agent does when it starts.
func (h *harness) listen() {
	h.t.Helper()
	listener, err := (&net.ListenConfig{}).Listen(h.t.Context(), "unix", h.socket)
	if err != nil {
		h.t.Fatal(err)
	}
	h.grpc = grpc.NewServer()
	hostproto.RegisterContainerLinkServer(h.grpc, h.server)
	go func() { _ = h.grpc.Serve(listener) }()
}

func (h *harness) accept() *linkConn {
	h.t.Helper()
	select {
	case c := <-h.server.conns:
		return c
	case <-time.After(10 * time.Second):
		h.t.Fatal("supervisor did not connect")
		return nil
	}
}

func (c *linkConn) send(t *testing.T, command *hostproto.SupervisorCommand) {
	t.Helper()
	if err := c.stream.Send(command); err != nil {
		t.Fatal(err)
	}
}

func (c *linkConn) configure(t *testing.T, handler string, slots int32) {
	t.Helper()
	workspace, err := filepath.Abs("testdata/workspace")
	if err != nil {
		t.Fatal(err)
	}
	c.send(t, &hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Configure{Configure: &hostproto.Configure{
		Handler: handler, Slots: slots, RunnerCommand: []string{"python3", "-m", "runner"}, WorkingDirectory: workspace,
	}}})
}

func (c *linkConn) run(t *testing.T, attempt, input string) {
	t.Helper()
	c.send(t, &hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Run{Run: &hostproto.RunAttempt{
		TaskId: "task-" + attempt, AttemptId: attempt,
		InputEncoding: hostproto.PayloadEncoding_PAYLOAD_ENCODING_JSON, Input: []byte(input),
	}}})
}

// until reads messages until match accepts one, and returns it with the
// output seen on the way, keyed by attempt.
func (c *linkConn) until(t *testing.T, match func(*hostproto.SupervisorMessage) bool) (*hostproto.SupervisorMessage, map[string]string) {
	t.Helper()
	output := map[string]string{}
	timeout := time.After(20 * time.Second)
	for {
		select {
		case m, ok := <-c.msgs:
			if !ok {
				t.Fatal("link closed")
			}
			if o := m.GetOutput(); o != nil {
				output[o.GetAttemptId()] += o.GetData()
			}
			if match(m) {
				return m, output
			}
		case <-timeout:
			t.Fatal("timed out waiting for a supervisor message")
		}
	}
}

func isReady(m *hostproto.SupervisorMessage) bool { return m.GetReady() != nil }

func finished(attempt string) func(*hostproto.SupervisorMessage) bool {
	return func(m *hostproto.SupervisorMessage) bool { return m.GetFinished().GetAttemptId() == attempt }
}

func TestSupervisorRunsAttemptsWithTheirOutput(t *testing.T) {
	h := startSupervisor(t)
	c := h.accept()
	started := time.Now()
	c.configure(t, "app:handle", 2)
	ready, output := c.until(t, isReady)
	t.Logf("configure to SlotsReady with 2 slots: %s", time.Since(started))
	if ready.GetReady().GetSlots() != 2 || !strings.Contains(output[""], "app imported") {
		t.Fatalf("ready %v, import output %q", ready, output[""])
	}

	c.run(t, "a1", `{"args": ["total", [1200, 3500, 800]]}`)
	m, output := c.until(t, finished("a1"))
	if got := string(m.GetFinished().GetSuccess().GetResult()); got != "5500" {
		t.Fatalf("result %q, finished %v", got, m)
	}
	if !strings.Contains(output["a1"], "summing 3 values") || !strings.Contains(output["a1"], "stderr line") {
		t.Fatalf("attempt output %q", output["a1"])
	}

	c.run(t, "a2", `{"args": ["nope"]}`)
	m, _ = c.until(t, finished("a2"))
	failure := m.GetFinished().GetFailure()
	if failure.GetKind() != hostproto.AttemptFailureKind_ATTEMPT_FAILURE_KIND_USER_ERROR || failure.GetError().GetType() != "ValueError" {
		t.Fatalf("failure %v", failure)
	}
}

func TestSupervisorCancelKillsTheSlotAndRestartsIt(t *testing.T) {
	h := startSupervisor(t)
	c := h.accept()
	c.configure(t, "app:handle", 1)
	c.until(t, isReady)

	c.run(t, "hung", `{"args": ["hang"]}`)
	c.until(t, func(m *hostproto.SupervisorMessage) bool {
		return m.GetOutput().GetAttemptId() == "hung" && strings.Contains(m.GetOutput().GetData(), "hanging")
	})
	cancelled := time.Now()
	c.send(t, &hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Cancel{Cancel: &hostproto.CancelSlot{AttemptId: "hung"}}})
	c.run(t, "next", `{"args": ["total", [1, 2]]}`)
	m, _ := c.until(t, func(m *hostproto.SupervisorMessage) bool { return m.GetFinished() != nil })
	if m.GetFinished().GetAttemptId() != "next" || string(m.GetFinished().GetSuccess().GetResult()) != "3" {
		t.Fatalf("after cancel: %v", m)
	}
	t.Logf("cancel to next result on the restarted slot: %s", time.Since(cancelled))
}

func TestSupervisorReportsACrashedSlotAndRecovers(t *testing.T) {
	h := startSupervisor(t)
	c := h.accept()
	c.configure(t, "app:handle", 1)
	c.until(t, isReady)

	c.run(t, "boom", `{"args": ["crash"]}`)
	m, output := c.until(t, finished("boom"))
	failure := m.GetFinished().GetFailure()
	if failure.GetKind() != hostproto.AttemptFailureKind_ATTEMPT_FAILURE_KIND_CRASHED || !strings.Contains(output["boom"], "crashing") {
		t.Fatalf("crash: %v, output %q", failure, output["boom"])
	}
	c.run(t, "after", `{"args": ["total", [4]]}`)
	m, _ = c.until(t, finished("after"))
	if string(m.GetFinished().GetSuccess().GetResult()) != "4" {
		t.Fatalf("after crash: %v", m)
	}
}

func TestSupervisorReportsLoadFailure(t *testing.T) {
	h := startSupervisor(t)
	c := h.accept()
	c.configure(t, "broken:handle", 2)
	m, _ := c.until(t, func(m *hostproto.SupervisorMessage) bool { return m.GetLoadFailed() != nil })
	if e := m.GetLoadFailed().GetError(); e.GetType() != "RuntimeError" || !strings.Contains(e.GetTraceback(), "broken.py") {
		t.Fatalf("load failure %v", e)
	}
	select {
	case err := <-h.result:
		h.result <- err
		if !errors.Is(err, ErrLoadFailed) {
			t.Fatalf("run returned %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("supervisor kept running after a load failure")
	}
}

func TestSupervisorDrainFinishesRunningAttempts(t *testing.T) {
	h := startSupervisor(t)
	c := h.accept()
	c.configure(t, "app:handle", 1)
	c.until(t, isReady)

	c.run(t, "slow", `{"args": ["sleep", 0.5]}`)
	c.send(t, &hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Drain{Drain: &hostproto.Drain{}}})
	m, _ := c.until(t, finished("slow"))
	if string(m.GetFinished().GetSuccess().GetResult()) != `"slept"` {
		t.Fatalf("drained attempt: %v", m)
	}
	select {
	case err := <-h.result:
		h.result <- err
		if err != nil {
			t.Fatalf("drain returned %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("supervisor did not exit after draining")
	}
}

func TestSupervisorReconnectRestatesRunningAttempts(t *testing.T) {
	h := startSupervisor(t)
	c := h.accept()
	c.configure(t, "app:handle", 2)
	c.until(t, isReady)
	c.run(t, "long", `{"args": ["sleep", 1.5]}`)
	time.Sleep(200 * time.Millisecond)
	close(c.end)

	c = h.accept()
	c.configure(t, "app:handle", 2)
	m, _ := c.until(t, isReady)
	if running := m.GetReady().GetRunningAttempts(); len(running) != 1 || running[0] != "long" {
		t.Fatalf("restated running attempts %v", running)
	}
	m, _ = c.until(t, finished("long"))
	if m.GetFinished().GetSuccess() == nil {
		t.Fatalf("attempt after reconnect: %v", m)
	}
}

// An attempt that finishes while no agent listens is still restated as
// running, so the next agent accepts its queued outcome.
func TestSupervisorRestatesAttemptsFinishedWhileDisconnected(t *testing.T) {
	h := startSupervisor(t)
	c := h.accept()
	c.configure(t, "app:handle", 1)
	c.until(t, isReady)
	c.run(t, "short", `{"args": ["sleep", 0.5]}`)
	time.Sleep(100 * time.Millisecond)
	h.grpc.Stop()
	time.Sleep(time.Second)

	h.listen()
	c = h.accept()
	c.configure(t, "app:handle", 1)
	m, _ := c.until(t, isReady)
	if running := m.GetReady().GetRunningAttempts(); len(running) != 1 || running[0] != "short" {
		t.Fatalf("restated running attempts %v", running)
	}
	m, _ = c.until(t, finished("short"))
	if m.GetFinished().GetSuccess() == nil {
		t.Fatalf("attempt finished while disconnected: %v", m)
	}
}
