// Command lazycloud-snapshotter serves containerd's snapshotter API with
// lazily read image layers on a host. It runs as root in its own systemd
// unit, which agent updates never restart: the FUSE mounts it serves die
// with it.
//
//	lazycloud-snapshotter [flags]
package main

import (
	"context"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"

	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
	"github.com/AmbientWare/lazycloud/internal/imagefs/snapshotter"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	set := flag.NewFlagSet("lazycloud-snapshotter", flag.ContinueOnError)
	root := set.String("root", "/var/lib/lazycloud-snapshotter", "snapshot metadata, snapshots and the frame cache")
	socket := set.String("socket", layersource.Socket, "Unix socket for containerd and the agent")
	cacheBytes := set.Int64("cache-bytes", 20<<30, "bound of the frame cache on disk")
	fetches := set.Int("fetches", 16, "frames read from the layer store at once")
	if err := set.Parse(args); err != nil {
		return 2
	}
	format, err := telemetry.LogFormatFromEnv(telemetry.LogText)
	if err != nil {
		fmt.Fprintln(os.Stderr, "snapshotter:", err)
		return 2
	}
	logger := telemetry.NewLogger(os.Stderr, format, "snapshotter")
	if os.Geteuid() != 0 {
		logger.Error("the snapshotter mounts layers and needs root")
		return 1
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	telemetryConfig, err := telemetry.ConfigFromEnv("snapshotter", "")
	if err != nil {
		logger.Error("invalid configuration", "error", err)
		return 2
	}
	tel, err := telemetry.New(ctx, telemetryConfig)
	if err != nil {
		logger.Error("telemetry failed", "error", err)
		return 1
	}
	defer func() { _ = tel.Shutdown(context.WithoutCancel(ctx)) }()
	metricsDone := make(chan error, 1)
	go func() { metricsDone <- tel.ServeMetrics(ctx, logger) }()
	cfg := snapshotter.Config{
		Root: *root, CacheBytes: *cacheBytes, Fetches: *fetches, AllowOther: true,
		HTTP: &http.Client{}, Registry: tel.Registry, Logger: logger,
	}
	err = snapshotter.Serve(ctx, cfg, *socket, func() {
		logger.Info("serving", "socket", *socket)
		if err := notifyReady(); err != nil {
			logger.Warn("tell systemd the snapshotter is ready", "error", err)
		}
	})
	stop()
	if metricsErr := <-metricsDone; metricsErr != nil {
		logger.Error("metrics listener failed", "error", metricsErr)
	}
	if err != nil {
		logger.Error("snapshotter failed", "error", err)
		return 1
	}
	return 0
}

// notifyReady tells systemd the socket accepts calls, so containerd and
// Docker start after it.
func notifyReady() error {
	path := os.Getenv("NOTIFY_SOCKET")
	if path == "" {
		return nil
	}
	conn, err := net.DialUnix("unixgram", nil, &net.UnixAddr{Name: path, Net: "unixgram"})
	if err != nil {
		return fmt.Errorf("dial systemd: %w", err)
	}
	defer func() { _ = conn.Close() }()
	if _, err := conn.Write([]byte("READY=1")); err != nil {
		return fmt.Errorf("notify systemd: %w", err)
	}
	return nil
}
