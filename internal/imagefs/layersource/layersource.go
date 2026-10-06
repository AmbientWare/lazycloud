// Package layersource is the agent's side of the host's snapshotter: the
// client of its LayerSources service, which hands it the presigned read URLs
// of the layers the host may mount, and the names a lazy pull uses.
package layersource

import (
	"context"
	"fmt"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// Socket is where the snapshotter serves containerd's snapshotter API
	// and LayerSources on a host.
	Socket = "/run/lazycloud-snapshotter/snapshotter.sock"
	// Root holds the snapshotter's metadata, snapshots and frame cache on a
	// host. containerd mounts the snapshots below it.
	Root = "/var/lib/lazycloud-snapshotter"
	// Snapshotter is the snapshotter's name in containerd's proxy plugins
	// and Docker's storage driver.
	Snapshotter = "lazycloud"
	// LazyLabel marks a layer pulled lazily. The pull sets it on each layer
	// descriptor; containerd passes it to the snapshotter's Prepare, which
	// then mounts the layer from its grant and reports it present, so the
	// layer is never downloaded. A layer without it unpacks as usual.
	LazyLabel = "containerd.io/snapshot/lazycloud.lazy"
)

// Grant is the presigned read URLs of one converted layer.
type Grant struct {
	Layer     imagefs.Digest
	IndexURL  string
	DataURL   string
	ExpiresAt time.Time
}

// Client calls one snapshotter.
type Client struct {
	conn    *grpc.ClientConn
	sources imagefsproto.LayerSourcesClient
}

// Dial returns a client of the snapshotter at socket. It connects on first
// use.
func Dial(socket string) (*Client, error) {
	conn, err := grpc.NewClient("unix:"+socket, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, fmt.Errorf("snapshotter client: %w", err)
	}
	return &Client{conn: conn, sources: imagefsproto.NewLayerSourcesClient(conn)}, nil
}

// Grant gives the snapshotter grants. Each replaces its layer's current
// grant unless that expires later. name is the container whose start they
// are for, empty for refreshes.
func (c *Client) Grant(ctx context.Context, name string, grants []Grant) error {
	request := &imagefsproto.GrantRequest{Name: name, Layers: make([]*imagefsproto.LayerGrant, len(grants))}
	for i, g := range grants {
		request.Layers[i] = &imagefsproto.LayerGrant{
			DiffId:    string(g.Layer),
			IndexUrl:  g.IndexURL,
			DataUrl:   g.DataURL,
			ExpiresAt: timestamppb.New(g.ExpiresAt),
		}
	}
	if _, err := c.sources.Grant(withTrace(ctx), request); err != nil {
		return fmt.Errorf("grant layers to the snapshotter: %w", err)
	}
	return nil
}

// FrameRead is one frame of the layer at a position in an image's layers.
type FrameRead struct {
	Layer, Frame uint32
}

func readsOut(reads []FrameRead) []*imagefsproto.FrameRead {
	out := make([]*imagefsproto.FrameRead, len(reads))
	for i, r := range reads {
		out[i] = &imagefsproto.FrameRead{Layer: r.Layer, Frame: r.Frame}
	}
	return out
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
func (c *Client) Prefetch(ctx context.Context, name string, layers []imagefs.Digest, reads []FrameRead) error {
	if _, err := c.sources.Prefetch(withTrace(ctx), &imagefsproto.PrefetchRequest{Name: name, Layers: digestsOut(layers), Reads: readsOut(reads)}); err != nil {
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
func (c *Client) EndTrace(ctx context.Context, name string) ([]FrameRead, bool, error) {
	ended, err := c.sources.EndTrace(ctx, &imagefsproto.EndTraceRequest{Name: name})
	if err != nil {
		return nil, false, fmt.Errorf("end a layer read trace: %w", err)
	}
	reads := make([]FrameRead, len(ended.GetReads()))
	for i, r := range ended.GetReads() {
		reads[i] = FrameRead{Layer: r.GetLayer(), Frame: r.GetFrame()}
	}
	return reads, ended.GetComplete(), nil
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
