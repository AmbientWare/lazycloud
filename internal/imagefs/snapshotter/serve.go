package snapshotter

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"time"

	snapshotsapi "github.com/containerd/containerd/api/services/snapshots/v1"
	"github.com/containerd/containerd/v2/contrib/snapshotservice"
	"google.golang.org/grpc"

	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
)

// stopGrace bounds how long Serve waits for calls in flight at shutdown.
const stopGrace = 10 * time.Second

// Serve runs the snapshotter on a Unix socket only root reaches, serving
// containerd's snapshotter API and LayerSources, until ctx ends. ready
// runs once the socket accepts calls.
func Serve(ctx context.Context, cfg Config, socket string, ready func()) error {
	s, err := New(ctx, cfg)
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
	imagefsproto.RegisterLayerSourcesServer(server, layerSources{grants: s.grants, frames: s.mounts.frames})
	served := make(chan error, 1)
	go func() { served <- server.Serve(listener) }()
	ready()
	select {
	case err := <-served:
		return fmt.Errorf("serve snapshotter: %w", err)
	case <-ctx.Done():
	}
	stopped := make(chan struct{})
	go func() {
		server.GracefulStop()
		close(stopped)
	}()
	select {
	case <-stopped:
	case <-time.After(stopGrace):
		server.Stop()
		<-stopped
	}
	<-served
	return nil
}
