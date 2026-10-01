// Package agent connects a host to the control plane and runs the containers
// the server assigns to it. It reports observed container state over the host
// session and moves task inputs, results and logs on separate calls. It has
// no database access; the server's durable state is the authority.
package agent

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/moby/moby/client"
	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/AmbientWare/lazycloud/internal/diskengine"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// Paths inside every workload container.
const (
	containerRuntimeDir = "/opt/lazycloud/runtime"
	containerSupervisor = "/opt/lazycloud/bin/supervisor"
	containerWorkspace  = "/workspace"
	containerLinkDir    = "/run/lazycloud"
	linkSocketName      = "agent.sock"
	// httpSocketName is where an HTTP workload's supervisor serves requests,
	// in the same directory as the link socket.
	httpSocketName = "http.sock"
	// containerAPIDir is a tmpfs in which the supervisor creates the
	// container API socket.
	containerAPIDir    = "/run/lazycloud-api"
	containerAPISocket = containerAPIDir + "/api.sock"
)

// maxIdleRequestConns bounds kept-alive connections to one container's
// supervisor.
const maxIdleRequestConns = 64

// maxSocketPath is the longest Unix socket path Linux accepts.
const maxSocketPath = 107

// maxMessageBytes bounds host connection messages. A claim can carry several
// 16 MiB inputs; the server limits how many it returns.
const maxMessageBytes = 512 << 20

// exitRetention is how long an exited container's report stays in every
// Hello, so a server that missed it learns the exit after reconnecting.
const exitRetention = 10 * time.Minute

// Config configures an agent.
type Config struct {
	// Server is the control plane's gRPC address, host:port.
	Server string
	// StateDir holds the host identity, the source cache and per-container
	// workspaces and sockets.
	StateDir string
	// SocketDir holds one directory per container with its link socket. Unix
	// socket paths are limited to 107 bytes, so it must be short.
	SocketDir string
	// JoinToken enrolls the host on first start.
	JoinToken string
	// RuntimeDir holds managed Python runtimes at <dir>/<python_version>.
	RuntimeDir string
	// SupervisorPath is the static supervisor binary mounted into containers.
	SupervisorPath string
	// OCIRuntime is the Docker runtime name, runc locally and runsc in
	// production.
	OCIRuntime string
	// GeeseFSPath is the pinned GeeseFS binary that mounts workspace volume
	// buckets; empty means the host mounts no volumes.
	GeeseFSPath string
	// MountImage is the image volume mount containers run GeeseFS in.
	MountImage string
	// BuildNetwork is the Docker network image builds run on. It must reach
	// the platform registry and the base images' registries.
	BuildNetwork string
	// Capacity is what the host offers; nil detects it.
	Capacity *hostproto.Capacity
	// Labels are added to every container the agent creates.
	Labels  map[string]string
	Version string
	Logger  *slog.Logger
	// Telemetry traces calls to the server and attempts; nil traces
	// nothing.
	Telemetry *telemetry.Telemetry
	// MetricsInterval paces container metric samples; zero means 5 s.
	MetricsInterval time.Duration
}

// Agent owns one host's connection and containers.
type Agent struct {
	cfg      Config
	log      *slog.Logger
	docker   *client.Client
	identity identity
	capacity *hostproto.Capacity
	bootID   string
	sources  *sourceCache
	images   *imageCache
	volumes  *volumes
	// diskQuota is whether Docker enforces writable layer limits here.
	diskQuota bool
	// diskEngine attaches durable disks; diskErr says why it cannot here.
	diskEngine *diskengine.Engine
	diskErr    error
	// diskLocks holds a mutex per disk id that serializes publishing.
	diskLocks sync.Map
	// releaseNow wakes the disk release loop.
	releaseNow chan struct{}
	// host carries claims, completions and logs on a connection separate from
	// the session, so large payloads never delay commands.
	host    hostproto.HostServiceClient
	control hostproto.HostServiceClient

	// work tracks every goroutine the agent starts; Run waits for them.
	work sync.WaitGroup
	ctx  context.Context

	metrics agentMetrics

	mu         sync.Mutex
	containers map[string]*container
	session    *sessionOut
}

// Run enrolls if needed, adopts containers left by a previous agent and
// keeps a session open until ctx ends. Containers keep running when the
// agent stops; the next agent adopts them.
func Run(ctx context.Context, cfg Config) error {
	if cfg.Logger == nil {
		cfg.Logger = slog.Default()
	}
	for _, dir := range []string{cfg.StateDir, filepath.Join(cfg.StateDir, "containers"), filepath.Join(cfg.StateDir, "sources")} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return fmt.Errorf("create state directory: %w", err)
		}
	}
	// A container id is 36 bytes; the socket path must fit sun_path.
	if n := len(filepath.Join(cfg.SocketDir, "01234567-89ab-cdef-0123-456789abcdef", linkSocketName)); n > maxSocketPath {
		return fmt.Errorf("socket directory %q gives %d-byte socket paths; the limit is %d", cfg.SocketDir, n, maxSocketPath)
	}
	if err := os.MkdirAll(cfg.SocketDir, 0o700); err != nil {
		return fmt.Errorf("create socket directory: %w", err)
	}
	if _, err := os.Stat(cfg.SupervisorPath); err != nil {
		return fmt.Errorf("supervisor binary: %w", err)
	}
	docker, err := client.New(client.FromEnv)
	if err != nil {
		return fmt.Errorf("create docker client: %w", err)
	}
	defer func() { _ = docker.Close() }()

	capacity := cfg.Capacity
	if capacity == nil {
		if capacity, err = detectCapacity(); err != nil {
			return err
		}
	}
	id, err := loadOrEnroll(ctx, cfg, capacity)
	if err != nil {
		return err
	}
	cfg.Logger = cfg.Logger.With("host_id", id.HostID)

	control, err := dialServer(cfg.Server, id.HostToken, cfg.Telemetry)
	if err != nil {
		return err
	}
	defer func() { _ = control.Close() }()
	payload, err := dialServer(cfg.Server, id.HostToken, cfg.Telemetry)
	if err != nil {
		return err
	}
	defer func() { _ = payload.Close() }()
	// Workload traffic has its own connection, so it shares flow control
	// with neither commands nor task payloads.
	traffic, err := dialServer(cfg.Server, id.HostToken, cfg.Telemetry)
	if err != nil {
		return err
	}
	defer func() { _ = traffic.Close() }()

	a := &Agent{
		cfg:        cfg,
		log:        cfg.Logger,
		docker:     docker,
		identity:   id,
		capacity:   capacity,
		bootID:     bootID(),
		sources:    &sourceCache{dir: filepath.Join(cfg.StateDir, "sources"), http: &http.Client{}},
		images:     &imageCache{docker: docker},
		host:       hostproto.NewHostServiceClient(payload),
		control:    hostproto.NewHostServiceClient(control),
		ctx:        ctx,
		containers: make(map[string]*container),
		releaseNow: make(chan struct{}, 1),
	}
	a.volumes = newVolumes(a)
	a.diskEngine = diskengine.New(filepath.Join(cfg.StateDir, "disks", "engine"), a.log.With("component", "disk"))
	if a.diskErr = a.diskEngine.Check(); a.diskErr != nil {
		a.log.Info("durable disks are unavailable on this host", "reason", a.diskErr)
	}
	if a.diskQuota, err = detectDiskQuota(ctx, docker); err != nil {
		return err
	}
	if !a.diskQuota {
		a.log.Warn("docker storage here cannot limit container disk; disk limits are not enforced")
	}
	var registerer prometheus.Registerer
	if cfg.Telemetry != nil {
		registerer = cfg.Telemetry.Registry
	}
	a.metrics = newAgentMetrics(a, registerer)
	defer a.shutdown()
	if err := a.removeBuildContainers(ctx); err != nil {
		return err
	}
	if err := a.adopt(ctx); err != nil {
		return err
	}
	a.recoverDisks(ctx)
	a.log.Info("agent started", "containers", len(a.containers), "cpu_millis", capacity.GetCpuMillis(), "memory_bytes", capacity.GetMemoryBytes())
	a.goOwned(a.pruneExited)
	data := &dataLink{a: a, client: hostproto.NewHostDataClient(traffic)}
	a.goOwned(data.run)
	a.goOwned(a.sampleUsage)
	a.goOwned(a.releaseLoop)
	a.sessions(ctx)
	return nil
}

// goOwned runs fn on a goroutine Run waits for.
func (a *Agent) goOwned(fn func(context.Context)) {
	a.work.Go(func() { fn(a.ctx) })
}

// shutdown stops every link server and waits for owned goroutines. The
// containers themselves keep running.
func (a *Agent) shutdown() {
	a.mu.Lock()
	containers := make([]*container, 0, len(a.containers))
	for _, c := range a.containers {
		containers = append(containers, c)
	}
	a.mu.Unlock()
	for _, c := range containers {
		c.detach()
	}
	a.work.Wait()
}

func (a *Agent) track(c *container) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.containers[c.id] = c
}

func (a *Agent) lookup(id string) *container {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.containers[id]
}

// pruneExited forgets exited containers after exitRetention.
func (a *Agent) pruneExited(ctx context.Context) {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		a.mu.Lock()
		for id, c := range a.containers {
			if c.expired(time.Now()) {
				delete(a.containers, id)
			}
		}
		a.mu.Unlock()
		a.volumes.pruneIdle(ctx)
	}
}

// hostToken attaches the host token to every call. The local transport is
// plaintext; TLS between agent and server is a gap in this slice.
type hostToken string

func (t hostToken) GetRequestMetadata(context.Context, ...string) (map[string]string, error) {
	return map[string]string{"authorization": "Bearer " + string(t)}, nil
}

func (hostToken) RequireTransportSecurity() bool { return false }

func dialServer(address, token string, t *telemetry.Telemetry) (*grpc.ClientConn, error) {
	options := []grpc.DialOption{
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(maxMessageBytes), grpc.MaxCallSendMsgSize(maxMessageBytes)),
	}
	if t != nil {
		options = append(options, t.GRPCDialOption())
	}
	if token != "" {
		options = append(options, grpc.WithPerRPCCredentials(hostToken(token)))
	}
	conn, err := grpc.NewClient(address, options...)
	if err != nil {
		return nil, fmt.Errorf("dial server %s: %w", address, err)
	}
	return conn, nil
}

func bootID() string {
	id, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return ""
	}
	return string(trimNewline(id))
}

func trimNewline(b []byte) []byte {
	for len(b) > 0 && (b[len(b)-1] == '\n' || b[len(b)-1] == '\r') {
		b = b[:len(b)-1]
	}
	return b
}

// sleep waits for d or ctx and reports whether ctx is still live.
func sleep(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

var errStopped = errors.New("container stopped")
