// Package layersource is the agent's client of the snapshotter's
// LayerSources service: it hands the snapshotter the presigned read URLs of
// the layers the host may mount.
package layersource

import (
	"context"
	"fmt"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
)

// Socket is where the snapshotter serves containerd's snapshotter API and
// LayerSources on a host.
const Socket = "/run/lazycloud-snapshotter/snapshotter.sock"

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
// grant unless that expires later.
func (c *Client) Grant(ctx context.Context, grants []Grant) error {
	request := &imagefsproto.GrantRequest{Layers: make([]*imagefsproto.LayerGrant, len(grants))}
	for i, g := range grants {
		request.Layers[i] = &imagefsproto.LayerGrant{
			DiffId:    string(g.Layer),
			IndexUrl:  g.IndexURL,
			DataUrl:   g.DataURL,
			ExpiresAt: timestamppb.New(g.ExpiresAt),
		}
	}
	if _, err := c.sources.Grant(ctx, request); err != nil {
		return fmt.Errorf("grant layers to the snapshotter: %w", err)
	}
	return nil
}

// Close closes the connection.
func (c *Client) Close() error {
	if err := c.conn.Close(); err != nil {
		return fmt.Errorf("close snapshotter client: %w", err)
	}
	return nil
}
