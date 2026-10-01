package agent

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// newTestContainer is a ready container whose agent talks to a test server
// and has no Docker container or supervisor behind it.
func newTestContainer(t *testing.T, server *hostServer) *container {
	t.Helper()
	grpcServer, address := server.serve(t, "127.0.0.1:0")
	conn, err := dialServer(Config{Server: address}, server.hostToken)
	if err != nil {
		t.Fatal(err)
	}
	dir, err := os.MkdirTemp("", "lca")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	a := &Agent{
		cfg:  Config{StateDir: dir, SocketDir: filepath.Join(dir, "s")},
		log:  slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug})),
		host: hostproto.NewHostServiceClient(conn),
		ctx:  ctx,
	}
	t.Cleanup(func() {
		cancel()
		a.work.Wait()
		_ = conn.Close()
		grpcServer.Stop()
		_ = os.RemoveAll(dir)
	})
	return a.newContainer(uuid.NewString(), "app:handle", 1, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
}

func succeeded(attempt string) *hostproto.AttemptFinished {
	return &hostproto.AttemptFinished{AttemptId: attempt, Outcome: &hostproto.AttemptFinished_Success{Success: &hostproto.TaskSuccess{
		Encoding: hostproto.PayloadEncoding_PAYLOAD_ENCODING_JSON, Result: []byte("1"),
	}}}
}

func (c *container) setRunning(attempts ...string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, attempt := range attempts {
		c.running[attempt] = struct{}{}
	}
}

// An outcome outlives a server outage longer than any fixed delivery bound,
// and its attempt keeps its slot until the server records it.
func TestCompletionOutlastsAServerOutage(t *testing.T) {
	const outage = 17 * time.Second
	server := newHostServer()
	c := newTestContainer(t, server)
	attempt := uuid.NewString()
	c.setRunning(attempt)
	began := time.Now()
	server.mu.Lock()
	server.completeOutage = began.Add(outage)
	server.mu.Unlock()

	c.onFinished(succeeded(attempt))
	time.Sleep(outage - time.Second)
	if running := c.snapshot().GetRunningAttempts(); !slices.Equal(running, []string{attempt}) {
		t.Fatalf("running attempts during the outage %v", running)
	}
	select {
	case r := <-server.completions:
		if r.GetAttemptId() != attempt {
			t.Fatalf("completion %v", r)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("outcome was not delivered after the outage")
	}
	t.Logf("delivered %s after a %s outage", time.Since(began), outage)
}

// The supervisor is untrusted: an outcome counts once, and only for an
// attempt the agent runs.
func TestContainerAcceptsOneOutcomePerRunningAttempt(t *testing.T) {
	server := newHostServer()
	c := newTestContainer(t, server)
	attempt := uuid.NewString()
	c.setRunning(attempt)

	c.onFinished(succeeded(uuid.NewString()))
	c.onFinished(succeeded(attempt))
	c.onFinished(succeeded(attempt))
	select {
	case r := <-server.completions:
		if r.GetAttemptId() != attempt {
			t.Fatalf("completion %v", r)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("no completion")
	}
	c.completions.Wait()
	c.onFinished(succeeded(attempt))
	select {
	case r := <-server.completions:
		t.Fatalf("unexpected completion %v", r)
	case <-time.After(500 * time.Millisecond):
	}
	if running := c.snapshot().GetRunningAttempts(); len(running) != 0 {
		t.Fatalf("running attempts %v", running)
	}
}

// A supervisor that reconnects while commands flow receives every command on
// one of its connections, and no reconnect panics the agent.
func TestLinkDeliversEveryCommandAcrossReconnects(t *testing.T) {
	const commands, reconnects = 2000, 40
	c := newTestContainer(t, newHostServer())
	l, err := listenLink(t.Context(), c, c.linkDir())
	if err != nil {
		t.Fatal(err)
	}
	c.link = l
	l.serve()
	t.Cleanup(func() { l.close(0) })
	conn, err := grpc.NewClient("unix:"+l.path, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	client := hostproto.NewContainerLinkClient(conn)

	var mu sync.Mutex
	received := map[string]bool{}
	var readers sync.WaitGroup
	connect := func(ctx context.Context) {
		stream, err := client.Connect(ctx)
		if err != nil {
			t.Error(err)
			return
		}
		readers.Go(func() {
			for {
				command, err := stream.Recv()
				if err != nil {
					return
				}
				if run := command.GetRun(); run != nil {
					mu.Lock()
					received[run.GetAttemptId()] = true
					mu.Unlock()
				}
			}
		})
	}
	ctx, cancel := context.WithCancel(t.Context())
	defer func() {
		cancel()
		readers.Wait()
	}()
	enqueued := make(chan struct{})
	go func() {
		defer close(enqueued)
		for i := range commands {
			l.enqueue(&hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Run{Run: &hostproto.RunAttempt{AttemptId: uuid.NewString()}}})
			if i%50 == 0 {
				time.Sleep(time.Millisecond)
			}
		}
	}()
	for range reconnects {
		connect(ctx)
		time.Sleep(time.Millisecond)
	}
	<-enqueued
	deadline := time.Now().Add(10 * time.Second)
	for {
		mu.Lock()
		n := len(received)
		mu.Unlock()
		if n == commands {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("supervisor received %d of %d commands; %d still queued", n, commands, len(l.queuedAttempts()))
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// A link closed before it served, as when the container fails to start,
// releases its socket.
func TestLinkClosedBeforeServingReleasesItsListener(t *testing.T) {
	c := newTestContainer(t, newHostServer())
	l, err := listenLink(t.Context(), c, c.linkDir())
	if err != nil {
		t.Fatal(err)
	}
	l.close(0)
	accepted := make(chan error, 1)
	go func() {
		conn, err := l.listener.Accept()
		if err == nil {
			_ = conn.Close()
		}
		accepted <- err
	}()
	select {
	case err := <-accepted:
		if !errors.Is(err, net.ErrClosed) {
			t.Fatalf("accept on a closed link: %v", err)
		}
	case <-time.After(2 * time.Second):
		_ = l.listener.Close()
		t.Fatal("the listener is still open")
	}
}
