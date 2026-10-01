// Command lazycloud-agent connects a host to the LazyCloud control plane and
// runs the workload containers it assigns.
//
//	lazycloud-agent join [flags]             run the agent
//	lazycloud-agent install-service [flags]  run it as a systemd service
//	lazycloud-agent --version
//
// Join flags default to LAZYCLOUD_* environment variables.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"runtime/debug"
	"strconv"
	"strings"
	"syscall"

	"github.com/AmbientWare/lazycloud/internal/agent"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// Exit statuses the service unit acts on.
const (
	// exitStopped (EX_CONFIG) means restarting cannot help: the credential
	// was revoked, the join was refused or preflight failed. The unit does
	// not restart it.
	exitStopped = 78
	// exitRestart (EX_TEMPFAIL) follows an installed update; the unit
	// restarts into the new release.
	exitRestart = 75
	exitUsage   = 2
)

// releaseVersion is set by the release build with -ldflags -X.
var releaseVersion string //nolint:gochecknoglobals // set by the linker, never at run time

const usage = `usage:
  lazycloud-agent join [flags]             run the agent in the foreground
  lazycloud-agent install-service [flags]  install and start the agent as a systemd service
  lazycloud-agent --version                print the agent version

Run "lazycloud-agent join -h" for the flags.
`

func main() {
	format, err := telemetry.LogFormatFromEnv(telemetry.LogText)
	if err != nil {
		fmt.Fprintln(os.Stderr, "agent:", err)
		os.Exit(exitUsage)
	}
	os.Exit(run(os.Args[1:], telemetry.NewLogger(os.Stderr, format, "agent")))
}

func run(args []string, logger *slog.Logger) int {
	if len(args) == 0 {
		fmt.Fprint(os.Stderr, usage)
		return exitUsage
	}
	switch args[0] {
	case "--version", "-version", "version":
		fmt.Println(version())
		return 0
	case "-h", "--help", "help":
		fmt.Print(usage)
		return 0
	case "join":
		cfg, err := parseJoin(args[1:])
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		if err != nil {
			logger.Error("invalid configuration", "error", err)
			return exitUsage
		}
		cfg.Logger = logger
		return join(cfg)
	case "install-service":
		if err := installService(args[1:]); err != nil {
			if errors.Is(err, flag.ErrHelp) {
				return 0
			}
			fmt.Fprintf(os.Stderr, "error: %v\n", err)
			return 1
		}
		return 0
	}
	fmt.Fprintf(os.Stderr, "unknown command %q\n%s", args[0], usage)
	return exitUsage
}

func join(cfg agent.Config) int {
	telemetryConfig, err := telemetry.ConfigFromEnv("agent", cfg.Version)
	if err != nil {
		cfg.Logger.Error("invalid configuration", "error", err)
		return exitUsage
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	tel, err := telemetry.New(ctx, telemetryConfig)
	if err != nil {
		cfg.Logger.Error("telemetry failed", "error", err)
		return 1
	}
	defer func() { _ = tel.Shutdown(context.WithoutCancel(ctx)) }()
	cfg.Telemetry = tel
	metricsDone := make(chan error, 1)
	go func() { metricsDone <- tel.ServeMetrics(ctx, cfg.Logger) }()
	err = agent.Run(ctx, cfg)
	stop()
	if metricsErr := <-metricsDone; metricsErr != nil {
		cfg.Logger.Error("metrics listener failed", "error", metricsErr)
	}
	switch {
	case err == nil:
		return 0
	case errors.Is(err, agent.ErrUpdateInstalled):
		cfg.Logger.Info("restarting into the new release")
		return exitRestart
	case errors.Is(err, agent.ErrCredentialRevoked):
		cfg.Logger.Error("host credential revoked", "error", err)
		return exitStopped
	case errors.Is(err, agent.ErrEnrollmentRefused), errors.Is(err, agent.ErrPreflightFailed):
		cfg.Logger.Error("the host cannot join", "error", err)
		return exitStopped
	}
	cfg.Logger.Error("agent failed", "error", err)
	return 1
}

// joinFlags are the join command's flags. install-service takes the same
// flags and passes them to the service.
type joinFlags struct {
	set       *flag.FlagSet
	cfg       agent.Config
	maxCPU    string
	maxMemory string
	maxGPUs   string
	gpuIDs    string
}

func newJoinFlags(name string) *joinFlags {
	executable, _ := os.Executable()
	release := filepath.Dir(executable)
	j := &joinFlags{set: flag.NewFlagSet(name, flag.ContinueOnError)}
	j.cfg = agent.Config{Labels: map[string]string{}, Version: version(), Executable: executable, AgentRoot: os.Getenv("LAZYCLOUD_AGENT_ROOT")}
	f, cfg := j.set, &j.cfg
	f.StringVar(&cfg.Server, "server", os.Getenv("LAZYCLOUD_SERVER"), "control plane gRPC address, host:port")
	f.StringVar(&cfg.ServerCA, "server-ca", os.Getenv("LAZYCLOUD_SERVER_CA"), "PEM bundle trusted for the server's certificate besides the system roots")
	f.BoolVar(&cfg.ServerPlaintext, "server-plaintext", os.Getenv("LAZYCLOUD_SERVER_PLAINTEXT") == "true", "dial a loopback server without TLS")
	f.StringVar(&cfg.StateDir, "state-dir", envOr("LAZYCLOUD_AGENT_STATE_DIR", "/var/lib/lazycloud/agent"), "host identity, source cache and container state")
	f.StringVar(&cfg.SocketDir, "socket-dir", envOr("LAZYCLOUD_AGENT_SOCKET_DIR", defaultSocketDir()), "short directory for per-container link sockets")
	f.StringVar(&cfg.JoinToken, "join-token", os.Getenv("LAZYCLOUD_JOIN_TOKEN"), "single-use token that enrolls the host on first start")
	f.StringVar(&cfg.JoinTokenFile, "join-token-file", os.Getenv("LAZYCLOUD_JOIN_TOKEN_FILE"), "file holding the join token; deleted once the host is enrolled")
	f.StringVar(&cfg.CloudHostID, "cloud-host-id", os.Getenv("LAZYCLOUD_CLOUD_HOST_ID"), "enroll this EC2 instance as the given host with its instance profile")
	f.StringVar(&cfg.IMDSEndpoint, "imds-endpoint", os.Getenv("LAZYCLOUD_IMDS_ENDPOINT"), "instance metadata endpoint; set, the agent reports Spot interruptions")
	f.StringVar(&cfg.Hostname, "hostname", os.Getenv("LAZYCLOUD_HOSTNAME"), "hostname reported at enrollment")
	f.StringVar(&cfg.RuntimeDir, "runtime-dir", envOr("LAZYCLOUD_RUNTIME_DIR", filepath.Join(release, "runtime")), "managed Python runtimes, one directory per version")
	f.StringVar(&cfg.SupervisorPath, "supervisor", envOr("LAZYCLOUD_SUPERVISOR", filepath.Join(release, "supervisor")), "static supervisor binary mounted into containers")
	f.StringVar(&cfg.OCIRuntime, "oci-runtime", envOr("LAZYCLOUD_OCI_RUNTIME", "runc"), "Docker runtime for workload containers (runsc in production)")
	f.StringVar(&cfg.GeeseFSPath, "geesefs", envOr("LAZYCLOUD_GEESEFS", filepath.Join(release, "geesefs")), "pinned GeeseFS binary that mounts volumes; volumes are unavailable without it")
	f.StringVar(&cfg.MountImage, "mount-image", envOr("LAZYCLOUD_MOUNT_IMAGE", agent.DefaultMountImage), "image that runs GeeseFS for volume mounts")
	f.StringVar(&cfg.BuildNetwork, "build-network", envOr("LAZYCLOUD_BUILD_NETWORK", "bridge"), "Docker network for image builds")
	f.StringVar(&j.maxCPU, "max-cpu", os.Getenv("LAZYCLOUD_MAX_CPU"), "CPU cores to offer, such as 2 or 1.5; default detects")
	f.StringVar(&j.maxMemory, "max-memory", os.Getenv("LAZYCLOUD_MAX_MEMORY"), "memory to offer, such as 16gib or 4096 (MB); default detects")
	f.StringVar(&j.maxGPUs, "max-gpus", os.Getenv("LAZYCLOUD_MAX_GPUS"), "offer the first N detected GPUs; 0 offers none")
	f.StringVar(&j.gpuIDs, "gpu-ids", os.Getenv("LAZYCLOUD_GPU_IDS"), "comma-separated GPU indexes or UUIDs to offer")
	f.Func("label", "key=value label added to every container (repeatable)", func(value string) error {
		key, val, ok := strings.Cut(value, "=")
		if !ok || key == "" {
			return fmt.Errorf("label %q is not key=value", value)
		}
		cfg.Labels[key] = val
		return nil
	})
	return j
}

func parseJoin(args []string) (agent.Config, error) {
	j := newJoinFlags("join")
	if err := j.parse(args); err != nil {
		return agent.Config{}, err
	}
	return j.cfg, nil
}

// parse reads and checks the flags.
func (j *joinFlags) parse(args []string) error {
	if err := j.set.Parse(args); err != nil {
		return fmt.Errorf("parse flags: %w", err)
	}
	if j.set.NArg() > 0 {
		return fmt.Errorf("unexpected argument %q", j.set.Arg(0))
	}
	cfg := &j.cfg
	if cfg.Server == "" {
		return errors.New("--server or LAZYCLOUD_SERVER is required")
	}
	if cfg.CloudHostID != "" && (cfg.JoinToken != "" || cfg.JoinTokenFile != "") {
		return errors.New("--cloud-host-id cannot be combined with a join token")
	}
	if cfg.CloudHostID != "" && cfg.IMDSEndpoint == "" {
		cfg.IMDSEndpoint = agent.DefaultIMDSEndpoint
	}
	var err error
	if j.maxCPU != "" {
		if cfg.Limits.CPUMillis, err = agent.ParseCPU(j.maxCPU); err != nil {
			return err
		}
	}
	if j.maxMemory != "" {
		if cfg.Limits.MemoryBytes, err = agent.ParseMemory(j.maxMemory); err != nil {
			return err
		}
	}
	if j.maxGPUs != "" {
		n, err := strconv.Atoi(j.maxGPUs)
		if err != nil || n < 0 {
			return errors.New("max gpus must be a number of GPUs")
		}
		cfg.Limits.GPUs = &n
	}
	for id := range strings.SplitSeq(j.gpuIDs, ",") {
		if id = strings.TrimSpace(id); id != "" {
			cfg.Limits.GPUIDs = append(cfg.Limits.GPUIDs, id)
		}
	}
	if cfg.Limits.GPUs != nil && len(cfg.Limits.GPUIDs) > 0 {
		return errors.New("--gpu-ids and --max-gpus cannot both be set")
	}
	if _, err := os.Stat(cfg.GeeseFSPath); err != nil {
		cfg.GeeseFSPath = ""
	}
	for _, path := range []*string{&cfg.StateDir, &cfg.RuntimeDir, &cfg.SupervisorPath, &cfg.JoinTokenFile, &cfg.GeeseFSPath} {
		if *path == "" {
			continue
		}
		abs, err := filepath.Abs(*path)
		if err != nil {
			return fmt.Errorf("resolve %s: %w", *path, err)
		}
		*path = abs
	}
	return nil
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func version() string {
	if releaseVersion != "" {
		return releaseVersion
	}
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return "unknown"
	}
	for _, setting := range info.Settings {
		if setting.Key == "vcs.revision" {
			return setting.Value
		}
	}
	return info.Main.Version
}

// defaultSocketDir is /run/lazycloud-agent for root and the user's runtime
// directory otherwise, both short enough for Unix socket paths.
func defaultSocketDir() string {
	if os.Geteuid() == 0 {
		return "/run/lazycloud-agent"
	}
	if dir := os.Getenv("XDG_RUNTIME_DIR"); dir != "" {
		return filepath.Join(dir, "lazycloud-agent")
	}
	return filepath.Join(os.TempDir(), fmt.Sprintf("lazycloud-agent-%d", os.Getuid()))
}
