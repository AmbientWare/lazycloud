// Package layersource is the agent's side of the host's snapshotter: the
// client of its LayerSources service, which hands it the presigned read URLs
// of the layers the host may mount, and of its DiskSources service, which
// serves disks' published generations, and of its VolumeSources service,
// which serves platform volumes; the names a lazy pull uses; and where the
// snapshotter, its cache and disks are.
package layersource

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// Socket is where the snapshotter serves containerd's snapshotter API
	// and LayerSources on a host.
	Socket = "/run/lazycloud-snapshotter/snapshotter.sock"
	// Root holds the snapshotter's metadata and snapshots on a host, and
	// the directory it serves disk generations in. containerd mounts the
	// snapshots below it.
	Root = "/var/lib/lazycloud-snapshotter"
	// CacheDir holds the frame cache and DiskRoot the disk engine's disks,
	// both on the host's data volume.
	CacheDir = hostproto.DataRoot + "/cache"
	DiskRoot = hostproto.DataRoot + "/disks"
	// Snapshotter is the snapshotter's name in containerd's proxy plugins
	// and Docker's storage driver.
	Snapshotter = "lazycloud"
	// Binary is the snapshotter the host runs, a copy of the Executable of
	// an agent release outside the releases, which updates prune; Unit is
	// its systemd unit.
	Executable = "lazycloud-snapshotter"
	Binary     = "/usr/local/lib/lazycloud/" + Executable
	Unit       = "lazycloud-snapshotter.service"
	// VolumeEndpoint is the loopback address of the snapshotter's S3
	// endpoint for platform volumes.
	VolumeEndpoint = "127.0.0.1:12121"
	// LazyLabel marks a layer pulled lazily. The pull sets it on each layer
	// descriptor; containerd passes it to the snapshotter's Prepare, which
	// then mounts the layer from its grant and reports it present, so the
	// layer is never downloaded. A layer without it unpacks as usual.
	LazyLabel = "containerd.io/snapshot/lazycloud.lazy"
)

// Install copies the snapshotter of the agent release executable is in to
// Binary unless Binary holds it, and reports whether it changed. An
// executable with no snapshotter beside it installs nothing.
func Install(executable string) (bool, error) {
	release, err := Replaced(executable)
	if release == nil || err != nil {
		return false, err
	}
	if err := os.MkdirAll(filepath.Dir(Binary), 0o755); err != nil { //nolint:gosec // Executables are world-readable.
		return false, fmt.Errorf("install the snapshotter: %w", err)
	}
	if err := os.WriteFile(Binary+".new", release, 0o755); err != nil { //nolint:gosec // An executable.
		return false, fmt.Errorf("install the snapshotter: %w", err)
	}
	if err := os.Rename(Binary+".new", Binary); err != nil {
		return false, fmt.Errorf("install the snapshotter: %w", err)
	}
	return true, nil
}

// Replaced returns the snapshotter of the agent release executable is in
// when Binary holds another, and nil when it holds that one or the release
// has none.
func Replaced(executable string) ([]byte, error) {
	exe, err := filepath.EvalSymlinks(executable)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("resolve the agent executable: %w", err)
	}
	release, err := os.ReadFile(filepath.Join(filepath.Dir(exe), Executable)) //nolint:gosec // The agent's own release.
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read the release's snapshotter: %w", err)
	}
	if current, err := os.ReadFile(Binary); err == nil && bytes.Equal(current, release) {
		return nil, nil
	}
	return release, nil
}

// Client calls one snapshotter.
type Client struct {
	conn    *grpc.ClientConn
	sources imagefsproto.LayerSourcesClient
	// Disks serves disks' published generations, and Volumes platform
	// volumes.
	Disks   imagefsproto.DiskSourcesClient
	Volumes imagefsproto.VolumeSourcesClient
}

// Dial returns a client of the snapshotter at socket. It connects on first
// use.
func Dial(socket string) (*Client, error) {
	conn, err := grpc.NewClient("unix:"+socket, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, fmt.Errorf("snapshotter client: %w", err)
	}
	return &Client{
		conn: conn, sources: imagefsproto.NewLayerSourcesClient(conn), Disks: imagefsproto.NewDiskSourcesClient(conn),
		Volumes: imagefsproto.NewVolumeSourcesClient(conn),
	}, nil
}

// Grant gives the snapshotter grants. Each replaces its layer's current
// grant unless that expires later. name is the container whose start they
// are for, empty for refreshes.
func (c *Client) Grant(ctx context.Context, name string, grants []*imagefsproto.LayerGrant) error {
	if _, err := c.sources.Grant(withTrace(ctx), &imagefsproto.GrantRequest{Name: name, Layers: grants}); err != nil {
		return fmt.Errorf("grant layers to the snapshotter: %w", err)
	}
	return nil
}

func digestsOut(layers []imagefs.Digest) []string {
	out := make([]string, len(layers))
	for i, l := range layers {
		out[i] = string(l)
	}
	return out
}

// Prefetch has the snapshotter fetch reads of layers, the image's layers
// base first, in the background as they mount, until StopPrefetch of name.
func (c *Client) Prefetch(ctx context.Context, name string, layers []imagefs.Digest, reads []*imagefsproto.FrameRead) error {
	if _, err := c.sources.Prefetch(withTrace(ctx), &imagefsproto.PrefetchRequest{Name: name, Layers: digestsOut(layers), Reads: reads}); err != nil {
		return fmt.Errorf("prefetch layers: %w", err)
	}
	return nil
}

// StopPrefetch ends the prefetch name, if it runs.
func (c *Client) StopPrefetch(ctx context.Context, name string) error {
	if _, err := c.sources.StopPrefetch(ctx, &imagefsproto.StopPrefetchRequest{Name: name}); err != nil {
		return fmt.Errorf("stop a prefetch: %w", err)
	}
	return nil
}

// StartTrace has the snapshotter record the frames of layers read until
// EndTrace with the same name.
func (c *Client) StartTrace(ctx context.Context, name string, layers []imagefs.Digest) error {
	if _, err := c.sources.StartTrace(ctx, &imagefsproto.StartTraceRequest{Name: name, Layers: digestsOut(layers)}); err != nil {
		return fmt.Errorf("start a layer read trace: %w", err)
	}
	return nil
}

// EndTrace stops the trace name and returns the frames read, each the
// first time, and whether every read reached it.
func (c *Client) EndTrace(ctx context.Context, name string) ([]*imagefsproto.FrameRead, bool, error) {
	ended, err := c.sources.EndTrace(ctx, &imagefsproto.EndTraceRequest{Name: name})
	if err != nil {
		return nil, false, fmt.Errorf("end a layer read trace: %w", err)
	}
	return ended.GetReads(), ended.GetComplete(), nil
}

// withTrace sends the trace of ctx's span, when sampled, so the
// snapshotter's work for the call's layers joins it.
func withTrace(ctx context.Context) context.Context {
	if tp := telemetry.TraceParentOf(ctx); tp != "" {
		return metadata.AppendToOutgoingContext(ctx, "traceparent", tp)
	}
	return ctx
}

// Close closes the connection.
func (c *Client) Close() error {
	if err := c.conn.Close(); err != nil {
		return fmt.Errorf("close snapshotter client: %w", err)
	}
	return nil
}
