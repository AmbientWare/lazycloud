// Package agent connects a host to the control plane and runs the containers
// the server assigns to it. It reports observed container state over the host
// session and moves task inputs, results and logs on separate calls. It has
// no database access; the server's durable state is the authority.
package agent

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/aws/aws-sdk-go-v2/feature/ec2/imds"
	"github.com/moby/moby/client"
	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
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

// dockerWait bounds how long the preflight waits for Docker to answer.
const dockerWait = 30 * time.Second

// exitRetention is how long an exited container's report stays in every
// Hello, so a server that missed it learns the exit after reconnecting.
const exitRetention = 10 * time.Minute

// Config configures an agent.
type Config struct {
	// Server is the control plane's gRPC address, host:port. The agent dials
	// it with TLS and verifies the server's certificate against the system
	// roots and ServerCA, a PEM bundle, when set. ServerPlaintext dials
	// without TLS and is accepted only for a loopback server.
	Server          string
	ServerCA        string
	ServerPlaintext bool
	// StateDir holds the host identity, the source cache and per-container
	// workspaces and sockets.
	StateDir string
	// SocketDir holds one directory per container with its link socket. Unix
	// socket paths are limited to 107 bytes, so it must be short.
	SocketDir string
	// JoinToken enrolls the host on first start. JoinTokenFile holds it
	// instead and is deleted once the host is enrolled.
	JoinToken     string
	JoinTokenFile string
	// CloudHostID enrolls a cloud instance with its instance-profile identity
	// in place of a join token.
	CloudHostID string
	// IMDSEndpoint is the instance metadata service. Set, the agent reports
	// Spot interruption notices.
	IMDSEndpoint string
	// Hostname is reported at enrollment; empty uses the system's.
	Hostname string
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
	// Limits caps the offered capacity.
	Limits Limits
	// Labels are added to every container the agent creates.
	Labels map[string]string
	// AgentRoot is the install root the service wrapper runs releases from;
	// with Executable inside its releases the agent can update itself.
	AgentRoot  string
	Executable string
	Version    string
	Logger     *slog.Logger
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
	// netfilters, snapshots and publishes bound network policy helpers,
	// checkpoints and filesystem publishes running at once.
	netfilters chan struct{}
	snapshots  chan struct{}
	publishes  chan struct{}
	// host carries claims, completions and logs on a connection separate from
	// the session, so large payloads never delay commands.
	host    hostproto.HostServiceClient
	control hostproto.HostServiceClient
	http    *http.Client
	// gpus are the offered devices; containers take free ones.
	gpus []gpuDevice
	// metadata is the cloud instance metadata service, nil off the cloud.
	metadata  *imds.Client
	updatable bool
	// committed is closed once the release on trial is committed.
	committed chan struct{}

	// work tracks every goroutine the agent starts; Run waits for them.
	work sync.WaitGroup
	ctx  context.Context
	// restart ends Run with a cause, as after installing an update.
	restart func(error)

	metrics agentMetrics

	mu         sync.Mutex
	containers map[string]*container
	session    *sessionOut
	// operations holds snapshots and filesystem publishes in flight.
	operations map[string]struct{}
	// interruption is the provider's standing reclaim notice.
	interruption *interruption
	// trial is the version on trial until a session commits it.
	trial    string
	updating bool
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

	machine, err := detect(ctx)
	if err != nil {
		return err
	}
	offered := resolveOffer(machine, cfg.Limits)
	offered.checks = append([]*hostproto.PreflightCheck{dockerCheck(ctx, docker)}, offered.checks...)
	var metadata *imds.Client
	if cfg.IMDSEndpoint != "" {
		metadata = newIMDS(cfg.IMDSEndpoint)
	}
	id, err := loadOrEnroll(ctx, cfg, offered, metadata)
	if err != nil {
		return err
	}
	// An enrolled host that fails a check may pass after a restart, as when
	// a driver loads late, so this is not ErrPreflightFailed.
	if failed := offered.failed(); len(failed) > 0 {
		return fmt.Errorf("preflight: %s", describeChecks(failed))
	}
	cfg.Logger = cfg.Logger.With("host_id", id.HostID)

	control, err := dialServer(cfg, id.HostToken)
	if err != nil {
		return err
	}
	defer func() { _ = control.Close() }()
	payload, err := dialServer(cfg, id.HostToken)
	if err != nil {
		return err
	}
	defer func() { _ = payload.Close() }()
	// Workload traffic has its own connection, so it shares flow control
	// with neither commands nor task payloads.
	traffic, err := dialServer(cfg, id.HostToken)
	if err != nil {
		return err
	}
	defer func() { _ = traffic.Close() }()

	ctx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	httpClient := &http.Client{}
	a := &Agent{
		cfg:        cfg,
		log:        cfg.Logger,
		docker:     docker,
		identity:   id,
		capacity:   offered.capacity,
		gpus:       offered.gpus,
		bootID:     bootID(),
		sources:    &sourceCache{dir: filepath.Join(cfg.StateDir, "sources"), http: httpClient},
		images:     &imageCache{docker: docker},
		host:       hostproto.NewHostServiceClient(payload),
		control:    hostproto.NewHostServiceClient(control),
		http:       httpClient,
		metadata:   metadata,
		updatable:  updatable(cfg.AgentRoot, cfg.Executable),
		committed:  make(chan struct{}),
		ctx:        ctx,
		restart:    cancel,
		containers: make(map[string]*container),
		releaseNow: make(chan struct{}, 1),
		netfilters: make(chan struct{}, maxNetfilterRuns),
		snapshots:  make(chan struct{}, maxSnapshots),
		publishes:  make(chan struct{}, maxPublishes),
		operations: make(map[string]struct{}),
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
	// Owned goroutines stop with ctx, so it ends before shutdown waits for them.
	defer func() {
		cancel(nil)
		a.shutdown()
	}()
	if err := a.removeBuildContainers(ctx); err != nil {
		return err
	}
	if err := a.adopt(ctx); err != nil {
		return err
	}
	a.recoverDisks(ctx)
	a.log.Info("agent started", "containers", len(a.containers), "cpu_millis", a.capacity.GetCpuMillis(),
		"memory_bytes", a.capacity.GetMemoryBytes(), "gpus", a.capacity.GetGpuCount(), "updatable", a.updatable)
	if a.updatable && readMarker(cfg.StateDir, TrialFile) == cfg.Version {
		a.trial = cfg.Version
		a.goOwned(a.watchTrial)
	}
	if metadata != nil {
		a.goOwned(a.watchInterruptions)
	}
	a.goOwned(a.pruneExited)
	data := &dataLink{a: a, client: hostproto.NewHostDataClient(traffic)}
	a.goOwned(data.run)
	a.goOwned(a.sampleUsage)
	a.goOwned(a.releaseLoop)
	err = a.sessions(ctx)
	if errors.Is(err, ErrCredentialRevoked) {
		// The machine was removed: nothing it runs belongs to anyone now.
		cleanup, done := context.WithTimeout(context.WithoutCancel(ctx), time.Minute)
		defer done()
		if rmErr := a.removeHostContainers(cleanup); rmErr != nil {
			a.log.Error("remove containers of a revoked host", "error", rmErr)
		}
	}
	if cause := context.Cause(ctx); cause != nil && !errors.Is(cause, context.Canceled) {
		return fmt.Errorf("agent stopped: %w", cause)
	}
	return err
}

func describeChecks(checks []*hostproto.PreflightCheck) string {
	messages := make([]string, 0, len(checks))
	for _, check := range checks {
		messages = append(messages, check.GetName()+": "+check.GetMessage())
	}
	return strings.Join(messages, "; ")
}

// dockerCheck waits briefly for Docker, which may still be starting at boot.
func dockerCheck(ctx context.Context, docker *client.Client) *hostproto.PreflightCheck {
	deadline := time.Now().Add(dockerWait)
	for {
		pingCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		_, err := docker.Ping(pingCtx, client.PingOptions{})
		cancel()
		if err == nil {
			return check("docker", true, "Docker is reachable", "")
		}
		if time.Now().After(deadline) || !sleep(ctx, time.Second) {
			return check("docker", false, "Docker is not reachable: "+err.Error(),
				"start Docker and check that docker info works for the agent's user")
		}
	}
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

// hostToken attaches the host token to every call.
type hostToken string

func (t hostToken) GetRequestMetadata(context.Context, ...string) (map[string]string, error) {
	return map[string]string{"authorization": "Bearer " + string(t)}, nil
}

func (hostToken) RequireTransportSecurity() bool { return false }

// ErrPlaintextRemote refuses a plaintext connection to a server that is not
// on this host: host tokens and workload data would cross the network in
// the clear.
var ErrPlaintextRemote = errors.New("plaintext is only allowed to a loopback server; use TLS")

// serverTransport is TLS verified against the system roots and the
// configured CA bundle, or plaintext to a loopback server.
func serverTransport(cfg Config) (credentials.TransportCredentials, error) {
	if cfg.ServerPlaintext {
		host, _, err := net.SplitHostPort(cfg.Server)
		if err != nil {
			host = cfg.Server
		}
		if ip := net.ParseIP(host); host != "localhost" && (ip == nil || !ip.IsLoopback()) {
			return nil, fmt.Errorf("server %s: %w", cfg.Server, ErrPlaintextRemote)
		}
		return insecure.NewCredentials(), nil
	}
	roots, err := x509.SystemCertPool()
	if err != nil {
		roots = x509.NewCertPool()
	}
	if cfg.ServerCA != "" {
		pem, err := os.ReadFile(cfg.ServerCA)
		if err != nil {
			return nil, fmt.Errorf("read server CA bundle: %w", err)
		}
		if !roots.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("server CA bundle %s holds no certificate", cfg.ServerCA)
		}
	}
	return credentials.NewTLS(&tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots}), nil
}

// dialServer connects to the control plane. Every agent connection to the
// server goes through it, so all of them share one transport policy.
func dialServer(cfg Config, token string) (*grpc.ClientConn, error) {
	transport, err := serverTransport(cfg)
	if err != nil {
		return nil, err
	}
	address := cfg.Server
	options := []grpc.DialOption{
		grpc.WithTransportCredentials(transport),
		grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(maxMessageBytes), grpc.MaxCallSendMsgSize(maxMessageBytes)),
	}
	if t := cfg.Telemetry; t != nil {
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

// Errors that end Run for good: restarting the agent cannot fix them, so its
// service does not restart it.
var (
	// ErrCredentialRevoked: the server refused the host token, as after the
	// machine was removed. The identity is deleted so a new join can enroll.
	ErrCredentialRevoked = errors.New("host credential revoked")
	// ErrEnrollmentRefused: the server refused the join token or identity.
	ErrEnrollmentRefused = errors.New("enrollment refused")
	// ErrPreflightFailed: an error-severity preflight check failed.
	ErrPreflightFailed = errors.New("preflight failed")
)
