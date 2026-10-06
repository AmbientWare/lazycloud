// Command lazycloud-snapshotter serves containerd's snapshotter API with
// lazily read image layers on a host. It runs as root in its own systemd
// unit, which agent updates never restart: the FUSE mounts it serves die
// with it.
//
//	lazycloud-snapshotter
package main

import (
	"context"
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

const (
	// cacheBytes bounds the frame cache on disk.
	cacheBytes = 20 << 30
	// fetches bounds the frames read from the layer store at once.
	fetches = 16
	// fillBytes is the largest layer, uncompressed, fetched whole in the
	// background once mounted.
	fillBytes = 256 << 20
)

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	if len(args) > 0 {
		fmt.Fprintln(os.Stderr, "usage: lazycloud-snapshotter")
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
	if telemetryConfig.OTLPEndpoint == "" {
		// The agent sends the snapshotter's spans, all in traces the server
		// sampled, to the server over its session.
		telemetryConfig.OTLPEndpoint, telemetryConfig.OTLPInsecure = "unix://"+telemetry.HostTraceSocket, true
	}
	tel, err := telemetry.New(ctx, telemetryConfig)
	if err != nil {
		logger.Error("telemetry failed", "error", err)
		return 1
	}
	defer func() { _ = tel.Shutdown(context.WithoutCancel(ctx)) }()
	// Every fetch slot keeps its connection to the store between frames.
	transport := http.DefaultTransport.(*http.Transport).Clone() //nolint:forcetypeassert // the standard library's transport
	transport.MaxIdleConnsPerHost = fetches
	cfg := snapshotter.Config{
		Root: layersource.Root, CacheBytes: cacheBytes, Fetches: fetches, FillBytes: fillBytes,
		HTTP: &http.Client{Transport: transport}, Logger: logger, Tracer: tel.Tracer(),
	}
	err = snapshotter.Serve(ctx, cfg, layersource.Socket, func() {
		logger.Info("serving", "socket", layersource.Socket)
		if err := notifyReady(); err != nil {
			logger.Warn("telling systemd the snapshotter is ready failed", "error", err)
		}
	})
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
