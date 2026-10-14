// Command lazycloud-snapshotter serves containerd's snapshotter API with
// lazily read image layers, the disk engine its disks' published
// generations, and volume mounts an S3 endpoint, on a host. It runs as root
// in its own systemd unit, which the agent restarts only on a host that
// runs no container: the FUSE mounts it serves die with it.
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
	"runtime/debug"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
	"github.com/AmbientWare/lazycloud/internal/imagefs/snapshotter"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// fetches bounds the frames read from the stores at once, half of them
	// in the background: 32 parallel 8 MiB reads fill a volume object at the
	// store's full speed.
	fetches = 64
	// fillBytes is the largest layer, uncompressed, fetched whole in the
	// background once mounted.
	fillBytes = 256 << 20
	// memoryLimit keeps the heap near the frames the fetches, the write
	// queue and the kept frames hold, so the snapshotter stays under 1 GiB
	// on a host of 8 GiB; collection alone would let it double.
	memoryLimit = 768 << 20
	// minVolumeDisk is the smallest instance store that stores volume
	// frames: EC2's instance stores below a 4xlarge's 600 GB sustain 130-190
	// MB/s of writes and read back slower than S3 delivers, while a 600 GB
	// one writes 500 MB/s and reads 1.6 GB/s. gp3 data volumes read back at
	// their 125 MiB/s baseline.
	minVolumeDisk = 500e9
	// instanceStore is the model EC2 gives NVMe instance store volumes.
	instanceStore = "Amazon EC2 NVMe Instance Storage"
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
	debug.SetMemoryLimit(memoryLimit)
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
	// Each disk's writes stall at its dirty budget, so the cache keeps the
	// budget of every disk slot the volume's free space holds free for
	// them. The cache starts empty; what else the volume holds, such as
	// disks' writes or, off a data volume, the host's own files, stays.
	var volume unix.Statfs_t
	if err := os.RemoveAll(layersource.CacheDir); err != nil {
		logger.Error("clearing the frame cache failed", "error", err)
		return 1
	}
	if err := os.MkdirAll(layersource.CacheDir, 0o700); err != nil {
		logger.Error("creating the frame cache directory failed", "error", err)
		return 1
	}
	if err := unix.Statfs(layersource.CacheDir, &volume); err != nil {
		logger.Error("reading the data volume's free space failed", "error", err)
		return 1
	}
	reserve := hostproto.DiskSlots(int64(volume.Bavail)*volume.Bsize) * hostproto.DiskDirtyBytes //nolint:gosec // Block counts fit an int64.
	cfg := snapshotter.Config{
		Root: layersource.Root, CacheDir: layersource.CacheDir, CacheBytes: hostproto.FrameCacheBytes,
		ReserveBytes: reserve, Fetches: fetches, FillBytes: fillBytes, VolumeAddr: layersource.VolumeEndpoint,
		HTTP: &http.Client{Transport: transport}, Logger: logger, Tracer: tel.Tracer(),
	}
	cfg.VolumeDisk = volumeDisk(layersource.CacheDir, volume)
	err = snapshotter.Serve(ctx, cfg, layersource.Socket, func() {
		logger.Info("serving", "socket", layersource.Socket, "stores_volume_frames", cfg.VolumeDisk)
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

// volumeDisk reports whether the data volume holding dir stores volume
// frames: an instance store of minVolumeDisk or more.
func volumeDisk(dir string, volume unix.Statfs_t) bool {
	var st unix.Stat_t
	if unix.Stat(dir, &st) != nil || float64(volume.Blocks)*float64(volume.Bsize) < minVolumeDisk {
		return false
	}
	model, err := os.ReadFile(fmt.Sprintf("/sys/dev/block/%d:%d/device/model", unix.Major(st.Dev), unix.Minor(st.Dev)))
	return err == nil && strings.TrimSpace(string(model)) == instanceStore
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
