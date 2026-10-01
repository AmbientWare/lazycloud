// Package acceptance runs cross-owner workflows against real PostgreSQL, the
// local object store, Docker and the managed Python runtime: server owners,
// scheduler loops and an agent in one test process.
package acceptance

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/moby/moby/client"
	"google.golang.org/grpc"

	"github.com/AmbientWare/lazycloud/internal/agent"
	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/edge"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/hostsession"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/scheduling"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

const (
	pythonVersion = "3.12"
	testLabel     = "lazycloud.acceptance"
)

// supervisorBinary is built once per test binary.
var supervisorBinary string //nolint:gochecknoglobals // built once in TestMain

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "lcaccept")
	if err != nil {
		panic(err)
	}
	supervisorBinary = filepath.Join(dir, "supervisor")
	build := exec.CommandContext(context.Background(), "go", "build", "-o", supervisorBinary, "../cmd/supervisor")
	build.Env = append(os.Environ(), "CGO_ENABLED=0")
	build.Stdout, build.Stderr = os.Stdout, os.Stderr
	if err := build.Run(); err != nil {
		panic(fmt.Sprintf("build supervisor: %v", err))
	}
	code := m.Run()
	_ = os.RemoveAll(dir)
	os.Exit(code)
}

// platform is one server, scheduler and agent over a fresh database.
type platform struct {
	t         *testing.T
	pool      *pgxpool.Pool
	control   *control.Control
	storage   *storage.Storage
	execution *execution.Execution
	edge      *edge.Edge
	workspace identity.Workspace
	token     string
	// edgeAddr is where the edge listens; client dials it whatever the host.
	edgeAddr  string
	client    *http.Client
	socketDir string
}

func (p *platform) port() string {
	_, port, _ := net.SplitHostPort(p.edgeAddr)
	return port
}

func runtimeDir(t *testing.T) string {
	t.Helper()
	dir, err := filepath.Abs("../.lazycloud/runtime")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, pythonVersion, "uvicorn")); err != nil {
		t.Skipf("the managed runtime is missing; run deploy/local/build-runtime.sh .lazycloud/runtime %s: %v", pythonVersion, err)
	}
	return dir
}

func startPlatform(t *testing.T) *platform {
	t.Helper()
	runtime := runtimeDir(t)
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelWarn}))
	pool := dbtest.New(t)
	p := &platform{
		t: t, pool: pool, control: control.NewControl(pool), storage: storage.NewStorage(pool, storagetest.Config()),
		execution: execution.NewExecution(pool),
	}
	ident := identity.NewIdentity(pool)
	if _, err := ident.CreateUser(ctx, "dev@lazycloud.test", false); err != nil {
		t.Fatal(err)
	}
	ws, err := ident.CreateWorkspace(ctx, "ws", "dev@lazycloud.test")
	if err != nil {
		t.Fatal(err)
	}
	p.workspace = ws
	if p.token, err = ident.CreateToken(ctx, "dev@lazycloud.test", "", "test"); err != nil {
		t.Fatal(err)
	}

	listener := database.NewListener(pool, logger, database.ChannelHost, database.ChannelTask, database.ChannelClaim,
		database.ChannelExecution, execution.ChannelContainerLog)
	edgeListener, err := (&net.ListenConfig{}).Listen(ctx, "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	p.edgeAddr = edgeListener.Addr().String()
	_, port, _ := net.SplitHostPort(p.edgeAddr)
	p.edge, err = edge.NewEdge(pool, ident, p.execution, listener, edge.Config{URL: "http://lazycloud.localhost:" + port}, logger)
	if err != nil {
		t.Fatal(err)
	}
	p.client = &http.Client{Transport: &http.Transport{
		DialContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, network, p.edgeAddr)
		},
		MaxIdleConnsPerHost: 1024,
	}}
	hosts := hostsession.NewServer(compute.NewCompute(pool), p.execution, p.storage, listener, hostsession.Config{
		ImageTemplate: "docker.io/library/python:{version}-slim", TouchInterval: 5 * time.Second,
	}, logger)
	grpcServer := grpc.NewServer(hosts.ServerOptions()...)
	hostproto.RegisterHostServiceServer(grpcServer, hosts)
	hostproto.RegisterHostDataServer(grpcServer, p.edge.DataServer())
	grpcListener, err := (&net.ListenConfig{}).Listen(ctx, "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	edgeServer := &http.Server{Handler: p.edge, ReadHeaderTimeout: 10 * time.Second}
	sched := scheduling.NewScheduling(pool, logger)
	planWake, cancelWake := listener.Subscribe(database.ChannelExecution, "")

	wg.Go(func() { _ = listener.Run(ctx) })
	wg.Go(func() { _ = p.edge.Run(ctx) })
	wg.Go(func() { _ = grpcServer.Serve(grpcListener) })
	wg.Go(func() { _ = edgeServer.Serve(edgeListener) })
	wg.Go(func() {
		defer cancelWake()
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			if _, err := p.execution.Plan(ctx, logger); err != nil && ctx.Err() == nil {
				t.Logf("plan: %v", err)
			}
			if _, err := p.execution.PlanServing(ctx, logger); err != nil && ctx.Err() == nil {
				t.Logf("plan serving: %v", err)
			}
			if _, err := sched.Place(ctx); err != nil && ctx.Err() == nil {
				t.Logf("place: %v", err)
			}
			select {
			case <-ctx.Done():
				return
			case <-planWake:
			case <-ticker.C:
			}
		}
	})
	wg.Go(func() {
		<-ctx.Done()
		hosts.Shutdown()
		p.edge.Shutdown()
		grpcServer.Stop()
		_ = edgeServer.Close()
		hosts.Wait()
	})

	join, _, err := compute.NewCompute(pool).CreateJoinToken(ctx, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	socketDir, err := os.MkdirTemp("", "lcs")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(socketDir) })
	p.socketDir = socketDir
	stateDir := t.TempDir()
	// Cleanups run last first: this one stops everything before the
	// database and directories go.
	t.Cleanup(func() {
		cancel()
		wg.Wait()
		removeContainers(t)
	})
	wg.Go(func() {
		err := agent.Run(ctx, agent.Config{
			Server: grpcListener.Addr().String(), StateDir: stateDir, SocketDir: socketDir, JoinToken: join,
			RuntimeDir: runtime, SupervisorPath: supervisorBinary, OCIRuntime: "runc",
			Capacity: &hostproto.Capacity{CpuMillis: 16000, MemoryBytes: 16 << 30},
			Labels:   map[string]string{testLabel: t.Name()}, Version: "test", Logger: logger,
		})
		if err != nil && ctx.Err() == nil {
			t.Errorf("agent: %v", err)
		}
	})
	return p
}

// removeContainers deletes the Docker containers the test's agent left.
func removeContainers(t *testing.T) {
	docker, err := client.New(client.FromEnv)
	if err != nil {
		t.Logf("docker client: %v", err)
		return
	}
	defer func() { _ = docker.Close() }()
	ctx := context.Background()
	list, err := docker.ContainerList(ctx, client.ContainerListOptions{
		All: true, Filters: client.Filters{}.Add("label", testLabel+"="+t.Name()),
	})
	if err != nil {
		t.Logf("list containers: %v", err)
		return
	}
	for _, c := range list.Items {
		_, _ = docker.ContainerRemove(ctx, c.ID, client.ContainerRemoveOptions{Force: true})
	}
}

// upload stores a source zip of files and returns its digest.
func (p *platform) upload(files map[string]string) string {
	p.t.Helper()
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	for name, body := range files {
		f, err := w.Create(name)
		if err != nil {
			p.t.Fatal(err)
		}
		if _, err := io.WriteString(f, body); err != nil {
			p.t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		p.t.Fatal(err)
	}
	sum := sha256.Sum256(buf.Bytes())
	digest := storage.Digest(sum)
	ctx := p.t.Context()
	for range 2 {
		up, err := p.storage.RegisterSource(ctx, p.workspace.ID, digest, int64(buf.Len()))
		if err != nil {
			p.t.Fatal(err)
		}
		if up.Present {
			return hex.EncodeToString(sum[:])
		}
		req, err := http.NewRequestWithContext(ctx, up.Upload.Method, up.Upload.URL, bytes.NewReader(buf.Bytes()))
		if err != nil {
			p.t.Fatal(err)
		}
		for k, v := range up.Upload.Headers {
			req.Header.Set(k, v)
		}
		req.ContentLength = int64(buf.Len())
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			p.t.Fatal(err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode/100 != 2 {
			p.t.Fatalf("upload source: %s", resp.Status)
		}
	}
	p.t.Fatal("source not stored after upload")
	return ""
}

// deploy deploys specs into app and returns its releases.
func (p *platform) deploy(app string, specs ...apitypes.FunctionSpec) apitypes.Deployment {
	p.t.Helper()
	d, err := p.control.Deploy(p.t.Context(), p.workspace.ID, app, apitypes.DeploymentRequest{Functions: specs})
	if err != nil {
		p.t.Fatalf("deploy: %v", err)
	}
	return d
}

func (p *platform) describe(app string, kind control.WorkloadKind, name string) apitypes.HttpWorkload {
	p.t.Helper()
	w, err := p.edge.Describe(p.t.Context(), p.workspace.ID, app, kind, name, nil)
	if err != nil {
		p.t.Fatalf("describe %s: %v", name, err)
	}
	return w
}

func (p *platform) request(method, url, token string, body io.Reader) (*http.Response, error) {
	req, err := http.NewRequestWithContext(p.t.Context(), method, url, body)
	if err != nil {
		return nil, err
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return p.client.Do(req)
}

// call sends a request and reads the whole response.
func (p *platform) call(method, url string, body string) (int, http.Header, string) {
	p.t.Helper()
	var reader io.Reader
	if body != "" {
		reader = bytes.NewBufferString(body)
	}
	resp, err := p.request(method, url, p.token, reader)
	if err != nil {
		p.t.Fatalf("%s %s: %v", method, url, err)
	}
	defer func() { _ = resp.Body.Close() }()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		p.t.Fatal(err)
	}
	return resp.StatusCode, resp.Header, string(data)
}

func spec(name, handler, source string, http *apitypes.HttpSpec) apitypes.FunctionSpec {
	return apitypes.FunctionSpec{
		Name: name, Handler: handler, Source: apitypes.SourceRef{Sha256: source},
		Image:     apitypes.ImageSpec{PythonVersion: apitypes.N312},
		Resources: apitypes.Resources{CpuMillis: 250, MemoryMib: 256},
		Http:      http,
	}
}

func liveContainers(t *testing.T, pool *pgxpool.Pool, release uuid.UUID) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(t.Context(), `select count(*) from containers where release_id = $1 and state <> 'stopped'`, release).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}
