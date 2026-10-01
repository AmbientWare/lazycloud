// Command agent connects a host to the LazyCloud control plane and runs the
// workload containers it assigns. Flags default to LAZYCLOUD_* environment
// variables.
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
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

func main() {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil)).With("component", "agent")
	cfg, err := parseConfig(os.Args[1:])
	if errors.Is(err, flag.ErrHelp) {
		return
	}
	if err != nil {
		logger.Error("invalid configuration", "error", err)
		os.Exit(2)
	}
	cfg.Logger = logger
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	if err := agent.Run(ctx, cfg); err != nil {
		logger.Error("agent failed", "error", err)
		stop()
		os.Exit(1)
	}
}

func parseConfig(args []string) (agent.Config, error) {
	flags := flag.NewFlagSet("agent", flag.ContinueOnError)
	cfg := agent.Config{Labels: map[string]string{}, Version: version()}
	executable, _ := os.Executable()
	flags.StringVar(&cfg.Server, "server", os.Getenv("LAZYCLOUD_SERVER"), "control plane gRPC address, host:port")
	flags.StringVar(&cfg.StateDir, "state-dir", envOr("LAZYCLOUD_AGENT_STATE_DIR", "/var/lib/lazycloud-agent"), "host identity, source cache and container state")
	flags.StringVar(&cfg.SocketDir, "socket-dir", envOr("LAZYCLOUD_AGENT_SOCKET_DIR", defaultSocketDir()), "short directory for per-container link sockets")
	flags.StringVar(&cfg.JoinToken, "join-token", os.Getenv("LAZYCLOUD_JOIN_TOKEN"), "single-use token that enrolls the host on first start")
	flags.StringVar(&cfg.RuntimeDir, "runtime-dir", envOr("LAZYCLOUD_RUNTIME_DIR", "/opt/lazycloud/runtimes"), "managed Python runtimes, one directory per version")
	flags.StringVar(&cfg.SupervisorPath, "supervisor", envOr("LAZYCLOUD_SUPERVISOR", filepath.Join(filepath.Dir(executable), "supervisor")), "static supervisor binary mounted into containers")
	flags.StringVar(&cfg.OCIRuntime, "oci-runtime", envOr("LAZYCLOUD_OCI_RUNTIME", "runc"), "Docker runtime for workload containers (runsc in production)")
	flags.StringVar(&cfg.GeeseFSPath, "geesefs", envOr("LAZYCLOUD_GEESEFS", filepath.Join(filepath.Dir(executable), "geesefs")), "pinned GeeseFS binary that mounts volumes; volumes are unavailable without it")
	flags.StringVar(&cfg.MountImage, "mount-image", envOr("LAZYCLOUD_MOUNT_IMAGE", agent.DefaultMountImage), "image that runs GeeseFS for volume mounts")
	cpu := flags.Int64("cpu-millis", envInt("LAZYCLOUD_CPU_MILLIS"), "offered CPU in millicores; 0 detects")
	memory := flags.Int64("memory-bytes", envInt("LAZYCLOUD_MEMORY_BYTES"), "offered memory in bytes; 0 detects")
	flags.Func("label", "key=value label added to every container (repeatable)", func(value string) error {
		key, val, ok := strings.Cut(value, "=")
		if !ok || key == "" {
			return fmt.Errorf("label %q is not key=value", value)
		}
		cfg.Labels[key] = val
		return nil
	})
	if err := flags.Parse(args); err != nil {
		return agent.Config{}, fmt.Errorf("parse flags: %w", err)
	}
	if cfg.Server == "" {
		return agent.Config{}, errors.New("-server or LAZYCLOUD_SERVER is required")
	}
	if (*cpu == 0) != (*memory == 0) {
		return agent.Config{}, errors.New("set both -cpu-millis and -memory-bytes, or neither")
	}
	if *cpu > 0 {
		cfg.Capacity = &hostproto.Capacity{CpuMillis: *cpu, MemoryBytes: *memory}
	}
	if _, err := os.Stat(cfg.GeeseFSPath); err != nil {
		cfg.GeeseFSPath = ""
	}
	for _, path := range []*string{&cfg.StateDir, &cfg.RuntimeDir, &cfg.SupervisorPath, &cfg.GeeseFSPath} {
		if *path == "" {
			continue
		}
		abs, err := filepath.Abs(*path)
		if err != nil {
			return agent.Config{}, fmt.Errorf("resolve %s: %w", *path, err)
		}
		*path = abs
	}
	return cfg, nil
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func envInt(key string) int64 {
	value, _ := strconv.ParseInt(os.Getenv(key), 10, 64)
	return value
}

func version() string {
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
