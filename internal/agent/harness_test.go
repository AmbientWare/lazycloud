//go:build hostruntime

package agent

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io/fs"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	cerrdefs "github.com/containerd/errdefs"
	"github.com/google/uuid"
	"github.com/moby/moby/client"
	"google.golang.org/grpc"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// The images tests start, pinned so that the image cache serves them.
const (
	testImage   = "docker.io/library/python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
	alpineImage = "docker.io/library/python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4"
	// ungrantedImage is pulled by no test, so no grant of it exists.
	ungrantedImage = "docker.io/library/python:3.11-alpine@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929"
)

// supervisorBinary is built once per test binary by TestMain.
var supervisorBinary string //nolint:gochecknoglobals // built once in TestMain

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "lcsupbin")
	if err != nil {
		panic(err)
	}
	supervisorBinary = filepath.Join(dir, "supervisor")
	build := exec.CommandContext(context.Background(), "go", "build", "-o", supervisorBinary, "../../cmd/supervisor")
	build.Env = append(os.Environ(), "CGO_ENABLED=0")
	build.Stdout, build.Stderr = os.Stdout, os.Stderr
	if err := build.Run(); err != nil {
		panic(fmt.Sprintf("build supervisor: %v", err))
	}
	store, removeStore, err := storagetest.Open(context.Background())
	if err != nil {
		panic(err)
	}
	layerBucket = store.LayerBucket
	if err := convertLazyImages(context.Background()); err != nil {
		_ = removeStore(context.Background())
		panic(fmt.Sprintf("convert the test images: %v", err))
	}
	code := m.Run()
	// The images the tests pulled read their layers from the test store,
	// whose buckets go next; left behind, they would serve later runs of
	// other tests unreadable files.
	for _, image := range lazyImages {
		out, err := exec.CommandContext(context.Background(), "docker", "image", "rm", "-f", image).CombinedOutput()
		if exec.CommandContext(context.Background(), "docker", "image", "inspect", image).Run() == nil {
			fmt.Fprintf(os.Stderr, "remove %s: %v: %s\n", image, err, out)
			code = max(code, 1)
		}
	}
	if err := removeStore(context.Background()); err != nil {
		fmt.Fprintln(os.Stderr, "remove the test buckets:", err)
		code = max(code, 1)
	}
	_ = os.RemoveAll(dir)
	os.Exit(code)
}

// platformImage is how the server answers a platform image the tests
// converted: hosts here pull it as named.
func platformImage(reference string) *hostproto.PlatformImage {
	return &hostproto.PlatformImage{Reference: reference, Image: reference, Platform: "linux/" + runtime.GOARCH, Layers: imageLayers[reference]}
}

// env is one test's server, source store, state directory and agents.
type env struct {
	t        *testing.T
	id       string
	server   *hostServer
	grpc     *grpc.Server
	address  string
	stateDir string
	docker   *client.Client
	source   *hostproto.Source
	// metricsInterval overrides the agent's sampling interval.
	metricsInterval time.Duration
	geesefs         string
	// running is the agent startAgent last started.
	running *runningAgent
}

func newEnv(t *testing.T) *env {
	t.Helper()
	docker, err := client.New(client.FromEnv)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := docker.Ping(t.Context(), client.PingOptions{}); err != nil {
		t.Fatalf("docker is required for agent tests: %v", err)
	}
	e := &env{t: t, id: "agent-test-" + uuid.NewString()[:8], server: newHostServer(), docker: docker}
	// As the server does once it converted them, sessions answer the
	// platform images the Hello names with their grants.
	e.server.answerPlatform = func(named []string) []*hostproto.PlatformImage {
		var platform []*hostproto.PlatformImage
		for _, ref := range named {
			platform = append(platform, platformImage(ref))
		}
		return platform
	}
	e.grpc, e.address = e.server.serve(t, "127.0.0.1:0")
	e.stateDir = shortDir(t)
	e.source = serveSource(t, "../supervisor/testdata/workspace")
	t.Cleanup(func() {
		e.grpc.Stop()
		e.removeContainers()
		_ = docker.Close()
	})
	return e
}

// removeContainers deletes every container this test created.
func (e *env) removeContainers() {
	ctx := context.Background()
	list, err := e.docker.ContainerList(ctx, client.ContainerListOptions{All: true, Filters: client.Filters{}.Add("label", "lazycloud.agent="+e.id)})
	if err != nil {
		e.t.Errorf("list test containers: %v", err)
		return
	}
	for _, c := range list.Items {
		// The agent may be removing a container it saw exit.
		if _, err := e.docker.ContainerRemove(ctx, c.ID, client.ContainerRemoveOptions{Force: true}); err != nil && !cerrdefs.IsNotFound(err) && !cerrdefs.IsConflict(err) {
			e.t.Errorf("remove test container: %v", err)
		}
	}
}

func (e *env) containers() int {
	list, err := e.docker.ContainerList(context.Background(), client.ContainerListOptions{All: true, Filters: client.Filters{}.Add("label", "lazycloud.agent="+e.id)})
	if err != nil {
		e.t.Fatal(err)
	}
	return len(list.Items)
}

type runningAgent struct {
	cancel context.CancelFunc
	done   chan error
}

// testRuntime is the Docker runtime workload containers run with:
// LAZYCLOUD_TEST_OCI_RUNTIME, runsc for gVisor, or runc.
func testRuntime() string {
	if runtime := os.Getenv("LAZYCLOUD_TEST_OCI_RUNTIME"); runtime != "" {
		return runtime
	}
	return "runc"
}

// startAgent runs an agent; configure adjusts its configuration.
func (e *env) startAgent(configure ...func(*Config)) *runningAgent {
	e.t.Helper()
	runtimeDir, err := filepath.Abs("../supervisor/testdata/runtime")
	if err != nil {
		e.t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	a := &runningAgent{cancel: cancel, done: make(chan error, 1)}
	cfg := Config{
		Server:          e.address,
		ServerPlaintext: true,
		StateDir:        e.stateDir,
		SocketDir:       filepath.Join(e.stateDir, "s"),
		JoinToken:       e.server.joinToken,
		RuntimeDir:      runtimeDir,
		SupervisorPath:  supervisorBinary,
		OCIRuntime:      testRuntime(),
		GeeseFSPath:     e.geesefs,
		TrustBundle:     HostTrustBundle(),
		BuildNetwork:    "host",
		Limits:          Limits{CPUMillis: 2000, MemoryBytes: 8 << 30},
		Labels:          map[string]string{"lazycloud.agent": e.id},
		Version:         "test",
		MetricsInterval: e.metricsInterval,
		Logger:          slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo})),
	}
	for _, fn := range configure {
		fn(&cfg)
	}
	go func() { a.done <- Run(ctx, cfg) }()
	e.t.Cleanup(a.stop)
	e.running = a
	return a
}

func (a *runningAgent) stop() {
	a.cancel()
	select {
	case <-a.done:
	case <-time.After(30 * time.Second):
		panic("agent did not stop")
	}
	a.done <- nil
}

// exited waits for Run to return on its own.
func (a *runningAgent) exited(t *testing.T) error {
	t.Helper()
	select {
	case err := <-a.done:
		a.done <- nil
		return err
	case <-time.After(60 * time.Second):
		t.Fatal("agent did not exit")
		return nil
	}
}

// agentExit yields Run's result if the running agent returns; failExited
// puts it back for stop and fails the test with it.
func (e *env) agentExit() <-chan error {
	if e.running == nil {
		return nil
	}
	return e.running.done
}

func (e *env) failExited(err error, waiting string) {
	e.running.done <- err
	e.t.Fatalf("agent exited before %s: %v", waiting, err)
}

func (e *env) enrollment() *hostproto.EnrollRequest {
	e.t.Helper()
	select {
	case r := <-e.server.enrolls:
		return r
	case err := <-e.agentExit():
		e.failExited(err, "enrolling")
		return nil
	case <-time.After(30 * time.Second):
		e.t.Fatal("agent did not enroll")
		return nil
	}
}

func (e *env) session() *serverSession {
	e.t.Helper()
	select {
	case s := <-e.server.sessions:
		return s
	case err := <-e.agentExit():
		e.failExited(err, "opening a session")
		return nil
	case <-time.After(30 * time.Second):
		e.t.Fatal("agent did not open a session")
		return nil
	}
}

func (s *serverSession) phase(t *testing.T, container string, phase hostproto.ContainerPhase) *hostproto.ContainerReport {
	t.Helper()
	return s.until(t, 120*time.Second, func(m *hostproto.HostMessage) bool {
		r := m.GetContainer()
		return r.GetContainerId() == container && r.GetPhase() == phase
	}).GetContainer()
}

// adoptedReady waits until an adopted container is ready: in the Hello when
// its supervisor reconnected before the session opened, or in a later
// report.
func (s *serverSession) adoptedReady(t *testing.T, container string) {
	t.Helper()
	for _, r := range s.hello.GetContainers() {
		if r.GetContainerId() == container && r.GetPhase() == hostproto.ContainerPhase_CONTAINER_PHASE_READY {
			return
		}
	}
	s.phase(t, container, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
}

func (e *env) startCommand(handler string, slots int32) *hostproto.ServerMessage {
	return &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Start{Start: &hostproto.StartContainer{
		ContainerId:   uuid.NewString(),
		Image:         testImage,
		Layers:        imageLayers[testImage],
		PythonVersion: "3.12",
		Source:        e.source,
		Resources:     &hostproto.Resources{CpuMillis: 1000, MemoryBytes: 256 << 20},
		Function:      &hostproto.FunctionWorkload{Handler: handler, Slots: slots},
	}}}
}

func stopCommand(container string, grace int32) *hostproto.ServerMessage {
	return &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Stop{Stop: &hostproto.StopContainer{
		ContainerId: container, GraceSeconds: grace,
	}}}
}

func cancelCommand(container, attempt string) *hostproto.ServerMessage {
	return &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Cancel{Cancel: &hostproto.CancelAttempt{
		ContainerId: container, AttemptId: attempt, Reason: hostproto.CancelReason_CANCEL_REASON_CANCELLED,
	}}}
}

// task queues a JSON task and returns its attempt id.
func (e *env) task(container, input string) string {
	attempt := uuid.NewString()
	e.server.enqueue(container, &hostproto.ClaimedTask{
		TaskId: uuid.NewString(), AttemptId: attempt, AttemptNumber: 1,
		InputEncoding: hostproto.PayloadEncoding_PAYLOAD_ENCODING_JSON, Input: []byte(input),
	})
	return attempt
}

func (e *env) completion(attempt string) *hostproto.CompleteTaskRequest {
	e.t.Helper()
	deadline := time.After(60 * time.Second)
	for {
		select {
		case r := <-e.server.completions:
			if r.GetAttemptId() == attempt {
				return r
			}
			e.t.Fatalf("unexpected completion for %s while waiting for %s: %v", r.GetAttemptId(), attempt, r)
		case <-deadline:
			e.t.Fatalf("no completion for %s", attempt)
			return nil
		}
	}
}

// serveSource zips dir and serves it the way a presigned URL would.
func serveSource(t *testing.T, dir string) *hostproto.Source {
	t.Helper()
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		name, _ := filepath.Rel(dir, path)
		f, err := w.Create(filepath.ToSlash(name))
		if err != nil {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		_, err = f.Write(data)
		return err
	})
	if err == nil {
		err = w.Close()
	}
	if err != nil {
		t.Fatal(err)
	}
	archive := buf.Bytes()
	sum := sha256.Sum256(archive)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(archive)
	}))
	t.Cleanup(server.Close)
	return &hostproto.Source{Sha256: hex.EncodeToString(sum[:]), Url: server.URL + "/source.zip"}
}
