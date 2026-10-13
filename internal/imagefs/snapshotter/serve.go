package snapshotter

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"

	snapshotsapi "github.com/containerd/containerd/api/services/snapshots/v1"
	"github.com/containerd/containerd/v2/contrib/snapshotservice"
	"google.golang.org/grpc"

	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
)

// stopGrace bounds how long Serve waits for calls in flight at shutdown.
const stopGrace = 10 * time.Second

// Serve runs the snapshotter on a Unix socket only root reaches, serving
// containerd's snapshotter API, LayerSources, DiskSources and
// VolumeSources, and with cfg.VolumeAddr the volumes' S3 endpoint, until
// ctx ends. ready runs once they accept calls.
func Serve(ctx context.Context, cfg Config, socket string, ready func()) error {
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
	var endpoint net.Listener
	if cfg.VolumeAddr != "" {
		if endpoint, err = lc.Listen(ctx, "tcp", cfg.VolumeAddr); err != nil {
			_ = listener.Close()
			return fmt.Errorf("listen on %s: %w", cfg.VolumeAddr, err)
		}
		s.volumes.addr = endpoint.Addr().String()
	}
	var serving sync.WaitGroup
	served := make(chan error, 2)
	serving.Go(func() { served <- server.Serve(listener) })
	if endpoint != nil {
		serving.Go(func() { served <- volumes.Serve(endpoint) })
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
	_ = volumes.Close()
	serving.Wait()
	return err
}
