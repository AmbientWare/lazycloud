package agent

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
	"github.com/AmbientWare/lazycloud/internal/imagefs/snapshotter"
)

// serveSnapshotter runs the host's snapshotter on socket until the test
// ends. Without root it mounts no layer, so its traces record no reads.
func serveSnapshotter(t *testing.T, socket string) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	ready, done := make(chan struct{}), make(chan error, 1)
	cfg := snapshotter.Config{
		Root: filepath.Join(filepath.Dir(socket), "root"), CacheBytes: 64 << 20, Fetches: 4,
		HTTP: http.DefaultClient, Logger: slog.New(slog.DiscardHandler),
	}
	go func() { done <- snapshotter.Serve(ctx, cfg, socket, func() { close(ready) }) }()
	select {
	case <-ready:
	case err := <-done:
		t.Fatal(err)
	}
	t.Cleanup(func() {
		cancel()
		if err := <-done; err != nil {
			t.Error(err)
		}
	})
}

// testLayerSources is the agent's side of a snapshotter the test runs.
func testLayerSources(t *testing.T) (*layerSources, *layersource.Client) {
	t.Helper()
	// Socket paths are short; a test's own directory may not be.
	dir, err := os.MkdirTemp("", "lcsnap")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	socket := filepath.Join(dir, "s.sock")
	serveSnapshotter(t, socket)
	client, err := layersource.Dial(socket)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	return newLayerSources(client), client
}

func layerGrant(diffID string, at time.Time) *hostproto.LayerGrant {
	return &hostproto.LayerGrant{
		DiffId: diffID, IndexUrl: "http://store/" + diffID + "/index", DataUrl: "http://store/" + diffID + "/data",
		ExpiresAt: timestamppb.New(at),
	}
}

func testDigest() string {
	return "sha256:" + strings.ReplaceAll(uuid.NewString()+uuid.NewString(), "-", "")
}

// startingWith is a start of a container recording its trace, with a
// prefetch and grants for layers.
func startingWith(layers ...string) *hostproto.StartContainer {
	spec := &hostproto.StartContainer{RecordTrace: true, Prefetch: &hostproto.ImageTrace{Reads: []*hostproto.FrameRead{{}}}}
	for _, l := range layers {
		spec.Layers = append(spec.Layers, layerGrant(l, time.Now().Add(time.Hour)))
	}
	return spec
}

// traceRunning reports whether the snapshotter still records name's trace,
// ending it if so, and whether the trace was complete.
func traceRunning(t *testing.T, client *layersource.Client, name string) (running, complete bool) {
	t.Helper()
	_, complete, err := client.EndTrace(t.Context(), name)
	if status.Code(err) == codes.NotFound {
		return false, false
	}
	if err != nil {
		t.Fatal(err)
	}
	return true, complete
}

// TestSharedStartsReportNoTrace: the snapshotter cannot tell apart the
// reads of two starts sharing a layer. A start sharing one with a live start
// records no trace, the earlier start's trace is left incomplete or marked
// shared, a start that ends stops its trace, and a start alone on its
// layers traces.
func TestSharedStartsReportNoTrace(t *testing.T) {
	l, client := testLayerSources(t)
	log := slog.New(slog.DiscardHandler)
	base := testDigest()

	if tracing, err := l.start(t.Context(), log, "c1", startingWith(base, testDigest())); err != nil || !tracing {
		t.Fatalf("the first start traces %v: %v", tracing, err)
	}
	if tracing, err := l.start(t.Context(), log, "c2", startingWith(base, testDigest())); err != nil || tracing {
		t.Fatalf("a start sharing a live start's layer traces %v: %v", tracing, err)
	}
	if running, complete := traceRunning(t, client, "c1"); !running || complete {
		t.Fatalf("the first start's trace runs %v, complete %v, after a start shared its layer", running, complete)
	}
	l.release(t.Context(), log, "c2")

	if tracing, err := l.start(t.Context(), log, "ends", startingWith(testDigest())); err != nil || !tracing {
		t.Fatalf("a start alone on its layers traces %v: %v", tracing, err)
	}
	l.release(t.Context(), log, "ends")
	if running, _ := traceRunning(t, client, "ends"); running {
		t.Fatal("an ended start left its trace running")
	}

	if _, err := l.start(t.Context(), log, "alone", startingWith(testDigest())); err != nil {
		t.Fatal(err)
	}
	if running, complete := traceRunning(t, client, "alone"); !running || !complete {
		t.Fatalf("a start alone on its layers traced %v, complete %v", running, complete)
	}

	// The snapshotter leaves complete a trace whose layers another start or
	// a platform image was granted, unmounted, before it began.
	l.release(t.Context(), log, "c1")
	traced := startingWith(base, testDigest())

	l.begin("a", layersOf(traced.GetLayers()))
	if err := client.Grant(t.Context(), "a", grantsIn(traced.GetLayers())); err != nil {
		t.Fatal(err)
	}
	if _, err := l.start(t.Context(), log, "b", startingWith(base)); err != nil {
		t.Fatal(err)
	}
	if err := client.StartTrace(t.Context(), "a", layersOf(traced.GetLayers())); err != nil {
		t.Fatal(err)
	}
	if running, complete := traceRunning(t, client, "a"); !running || !complete {
		t.Fatalf("the snapshotter's trace runs %v, complete %v", running, complete)
	}
	shared := func(container string) bool {
		l.smu.Lock()
		defer l.smu.Unlock()
		return l.startups[container].shared
	}
	if !shared("a") {
		t.Fatal("a start a later start shared before its trace began is not marked shared")
	}

	l.release(t.Context(), log, "a")
	l.release(t.Context(), log, "b")
	l.begin("c", []imagefs.Digest{imagefs.Digest(base)})
	if err := l.grant(t.Context(), "platform:builder", []*hostproto.LayerGrant{layerGrant(base, time.Now().Add(time.Hour))}); err != nil {
		t.Fatal(err)
	}
	if !shared("c") {
		t.Fatal("a start whose layer a platform image was granted is not marked shared")
	}
}

// TestStartupTracesEndWithinTheWindow: a container that serves nothing
// ends its trace traceWindow after it began, and one that exits first ends
// the wait at once.
func TestStartupTracesEndWithinTheWindow(t *testing.T) {
	l, client := testLayerSources(t)
	log := slog.New(slog.DiscardHandler)
	never := make(chan struct{})

	if tracing, err := l.start(t.Context(), log, "idle", startingWith(testDigest())); err != nil || !tracing {
		t.Fatalf("the start traces %v: %v", tracing, err)
	}
	const left = 300 * time.Millisecond
	l.smu.Lock()
	l.startups["idle"].began = time.Now().Add(left - traceWindow)
	l.smu.Unlock()
	began := time.Now()
	l.await(t.Context(), log, "idle", never)
	if waited := time.Since(began); waited < left-50*time.Millisecond || waited > left+5*time.Second {
		t.Fatalf("an idle container's trace ended after %s with %s of its window left", waited, left)
	}
	if running, _ := traceRunning(t, client, "idle"); running {
		t.Fatal("the trace still ran after its window")
	}

	if _, err := l.start(t.Context(), log, "serves", startingWith(testDigest())); err != nil {
		t.Fatal(err)
	}
	served := make(chan struct{})
	time.AfterFunc(left, func() { close(served) })
	began = time.Now()
	l.await(t.Context(), log, "serves", served)
	if waited := time.Since(began); waited < left-50*time.Millisecond || waited > left+5*time.Second {
		t.Fatalf("a trace ended %s after its start, %s before the container first served", waited, left)
	}
	if running, _ := traceRunning(t, client, "serves"); running {
		t.Fatal("the trace still ran after the container first served")
	}

	if _, err := l.start(t.Context(), log, "exits", startingWith(testDigest())); err != nil {
		t.Fatal(err)
	}
	time.AfterFunc(50*time.Millisecond, func() { l.release(context.Background(), log, "exits") })
	began = time.Now()
	if trace := l.await(t.Context(), log, "exits", never); trace != nil || time.Since(began) > 5*time.Second {
		t.Fatalf("an exited container reported %v after %s", trace, time.Since(began))
	}
}

// TestRefreshesOutlastTheSnapshotter: a refresh the snapshotter does not
// take is kept and retried until it does, and a start whose grants it
// refuses fails.
func TestRefreshesOutlastTheSnapshotter(t *testing.T) {
	dir, err := os.MkdirTemp("", "lcsnap")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	socket := filepath.Join(dir, "s.sock")
	client, err := layersource.Dial(socket)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	l := newLayerSources(client)
	ctx, cancel := context.WithCancel(t.Context())
	looped := make(chan struct{})
	go func() {
		defer close(looped)
		l.refreshLoop(ctx, slog.New(slog.DiscardHandler))
	}()
	t.Cleanup(func() { cancel(); <-looped })
	pending := func() int {
		l.mu.Lock()
		defer l.mu.Unlock()
		return len(l.pending)
	}

	l.refresh([]*hostproto.LayerGrant{layerGrant(testDigest(), time.Now().Add(time.Hour))})
	time.Sleep(300 * time.Millisecond)
	if pending() != 1 {
		t.Fatal("a refresh the snapshotter could not take was dropped")
	}
	serveSnapshotter(t, socket)
	for deadline := time.Now().Add(30 * time.Second); pending() != 0; time.Sleep(50 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("the refresh never reached the snapshotter once it served")
		}
	}

	refused := startingWith(testDigest())
	refused.Layers[0].IndexUrl = "ftp://store/index"
	if _, err := l.start(t.Context(), slog.New(slog.DiscardHandler), "refused", refused); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("a start with grants the snapshotter refuses gave %v", err)
	}
}

// TestStartupTracesEndWithTheFirstTask: the host's snapshotter traces a
// start until its first task, request or command ends, the agent reports
// the frames read, and a later start prefetches them. No other package
// unpacks this image without the snapshotter, so its layers mount lazily
// and reads reach FUSE.
func TestStartupTracesEndWithTheFirstTask(t *testing.T) {
	const image = "python:3.12-alpine"
	e := newEnv(t)
	e.startAgent()
	s := e.session()
	command := &hostproto.PodWorkload{Command: []string{"python3", "-c", "import email, http.client, json, sqlite3"}}

	start := withImage(e.podCommand(command), image)
	id := start.GetStart().GetContainerId()
	start.GetStart().RecordTrace = true
	s.send(t, start)
	reported := s.until(t, 120*time.Second, func(m *hostproto.HostMessage) bool { return m.GetStartupTrace() != nil }).GetStartupTrace()
	if reported.GetContainerId() != id || len(reported.GetTrace().GetReads()) == 0 {
		t.Fatalf("the host reported %v", reported)
	}
	s.phase(t, id, exited)

	next := withImage(e.podCommand(command), image)
	next.GetStart().Prefetch = reported.GetTrace()
	s.send(t, next)
	if exit := s.phase(t, next.GetStart().GetContainerId(), exited).GetExit(); exit.GetReason() != hostproto.ExitReason_EXIT_REASON_EXITED || exit.GetExitCode() != 0 {
		t.Fatalf("a start prefetching the trace exited %v", exit)
	}
}
