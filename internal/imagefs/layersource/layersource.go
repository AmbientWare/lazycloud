// Package layersource is the agent's side of the host's snapshotter: the
// client of its LayerSources service, which hands it the presigned read URLs
// of the layers the host may mount, and of its DiskSources service, which
// serves disks' published generations; the names a lazy pull uses; and
// where the host keeps its data volume.
package layersource

import (
	"context"
	"fmt"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

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
	// DataRoot is the host's data volume. It holds the frame cache, at
	// CacheDir, and the disk engine's disks, at DiskRoot, whose unpublished
	// writes the cache leaves room for.
	DataRoot = "/var/lib/lazycloud-data"
	CacheDir = DataRoot + "/cache"
	DiskRoot = DataRoot + "/disks"
	// CacheBytes bounds the frame cache.
	CacheBytes = 32 << 30
	// DiskDirtyBytes is the room each disk a host holds keeps on the data
	// volume for writes not yet published. A disk publishes once half of
	// it is used.
	DiskDirtyBytes = 8 << 30
	// Snapshotter is the snapshotter's name in containerd's proxy plugins
	// and Docker's storage driver.
	Snapshotter = "lazycloud"
	// LazyLabel marks a layer pulled lazily. The pull sets it on each layer
	// descriptor; containerd passes it to the snapshotter's Prepare, which
	// then mounts the layer from its grant and reports it present, so the
	// layer is never downloaded. A layer without it unpacks as usual.
	LazyLabel = "containerd.io/snapshot/lazycloud.lazy"
)

// Client calls one snapshotter.
type Client struct {
	conn    *grpc.ClientConn
	sources imagefsproto.LayerSourcesClient
	disks   imagefsproto.DiskSourcesClient
}

// Dial returns a client of the snapshotter at socket. It connects on first
// use.
func Dial(socket string) (*Client, error) {
	conn, err := grpc.NewClient("unix:"+socket, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, fmt.Errorf("snapshotter client: %w", err)
	}
	return &Client{conn: conn, sources: imagefsproto.NewLayerSourcesClient(conn), disks: imagefsproto.NewDiskSourcesClient(conn)}, nil
}

// GrantDisk gives the snapshotter the credential it reads disk's objects
// with.
func (c *Client) GrantDisk(ctx context.Context, disk string, grant *imagefsproto.DiskGrant) error {
	if _, err := c.disks.GrantDisk(ctx, &imagefsproto.GrantDiskRequest{DiskId: disk, Grant: grant}); err != nil {
		return fmt.Errorf("grant disk %s to the snapshotter: %w", disk, err)
	}
	return nil
}

// ServeDisk serves a disk generation and returns its file.
func (c *Client) ServeDisk(ctx context.Context, req *imagefsproto.ServeDiskRequest) (string, error) {
	served, err := c.disks.ServeDisk(ctx, req)
	if err != nil {
		return "", fmt.Errorf("serve disk %s generation %d: %w", req.GetDiskId(), req.GetGeneration(), err)
	}
	return served.GetPath(), nil
}

// ReleaseDisk stops serving every generation of disk but keep, 0 for none.
func (c *Client) ReleaseDisk(ctx context.Context, disk string, keep int64) error {
	if _, err := c.disks.ReleaseDisk(ctx, &imagefsproto.ReleaseDiskRequest{DiskId: disk, Keep: keep}); err != nil {
		return fmt.Errorf("release disk %s from the snapshotter: %w", disk, err)
	}
	return nil
}

// DiskReads returns disk's start trace, empty until its first minute has
// passed, and the frames the cache holds for it, most recent first.
func (c *Client) DiskReads(ctx context.Context, disk string) (start, recent []uint32, err error) {
	reads, err := c.disks.DiskReads(ctx, &imagefsproto.DiskReadsRequest{DiskId: disk})
	if err != nil {
		return nil, nil, fmt.Errorf("read disk %s's reads: %w", disk, err)
	}
	return reads.GetStartFrames(), reads.GetRecentFrames(), nil
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
