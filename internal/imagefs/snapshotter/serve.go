package snapshotter

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"runtime/debug"
	"sync"
	"time"

	snapshotsapi "github.com/containerd/containerd/api/services/snapshots/v1"
	"github.com/containerd/containerd/v2/contrib/snapshotservice"
	"golang.org/x/net/netutil"
	"google.golang.org/grpc"

	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
)

const (
	// stopGrace bounds how long Serve waits for calls in flight at shutdown.
	stopGrace = 10 * time.Second
	// memoryHeadroom is what the heap holds besides frames: the HTTP
	// buffers, the index and metadata of layers and disks.
	memoryHeadroom = 64 << 20
)

// Serve runs the snapshotter on a Unix socket only root reaches, serving
// containerd's snapshotter API, LayerSources, DiskSources and
// VolumeSources, and the volumes' S3 endpoint on cfg.VolumeAddr, until ctx
// ends. ready runs once they accept calls.
func Serve(ctx context.Context, cfg Config, socket string, ready func()) error {
	// The heap holds a frame per fetch in flight, per frame queued for the
	// writer and per kept frame; without the limit collection lets it
	// grow to twice that.
	debug.SetMemoryLimit(int64(cfg.Fetches+writeQueue+keptFrames)*volumeFrame + memoryHeadroom)
	s, err := newSnapshotter(ctx, cfg)
	if err != nil {
		return err
	}
	defer func() { _ = s.Close() }()
	if err := os.MkdirAll(filepath.Dir(socket), 0o700); err != nil {
		return fmt.Errorf("create socket directory: %w", err)
	}
	if err := os.Remove(socket); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("remove stale socket: %w", err)
	}
	var lc net.ListenConfig
	listener, err := lc.Listen(ctx, "unix", socket)
	if err != nil {
		return fmt.Errorf("listen on %s: %w", socket, err)
	}
	if err := os.Chmod(socket, 0o600); err != nil {
		_ = listener.Close()
		return fmt.Errorf("restrict socket: %w", err)
	}
	server := grpc.NewServer()
	snapshotsapi.RegisterSnapshotsServer(server, snapshotservice.FromSnapshotter(s))
	imagefsproto.RegisterLayerSourcesServer(server, layerSources{cache: s.cache})
	imagefsproto.RegisterDiskSourcesServer(server, diskSources{disks: s.disks})
	imagefsproto.RegisterVolumeSourcesServer(server, volumeSources{volumes: s.volumes})
	// Forwarded writes and reads run as long as they take.
	volumes := &http.Server{Handler: s.volumes, ReadHeaderTimeout: stopGrace}
	endpoint, err := lc.Listen(ctx, "tcp", cfg.VolumeAddr)
	if err != nil {
		_ = listener.Close()
		return fmt.Errorf("serve the volume endpoint: %w", err)
	}
	peers := &http.Server{Handler: peerServer{v: s.volumes}, ReadHeaderTimeout: stopGrace}
	var peerListener net.Listener
	if cfg.PeerAddr != "" {
		if peerListener, err = lc.Listen(ctx, "tcp", cfg.PeerAddr); err != nil {
			_ = listener.Close()
			_ = endpoint.Close()
			return fmt.Errorf("serve volume peers: %w", err)
		}
		peerListener = netutil.LimitListener(peerListener, peerConnections)
	}
	var serving sync.WaitGroup
	served := make(chan error, 3)
	serving.Go(func() { served <- server.Serve(listener) })
	serving.Go(func() { served <- volumes.Serve(endpoint) })
	if peerListener != nil {
		serving.Go(func() { served <- peers.Serve(peerListener) })
	}
	ready()
	select {
	case err = <-served:
		err = fmt.Errorf("serve snapshotter: %w", err)
	case <-ctx.Done():
	}
	// Stop ends a graceful stop still waiting at the grace.
	force := time.AfterFunc(stopGrace, server.Stop)
	server.GracefulStop()
	force.Stop()
	shutdown, cancel := context.WithTimeout(context.WithoutCancel(ctx), stopGrace)
	defer cancel()
	if volumes.Shutdown(shutdown) != nil {
		_ = volumes.Close()
	}
	if peers.Shutdown(shutdown) != nil {
		_ = peers.Close()
	}
	serving.Wait()
	return err
}
