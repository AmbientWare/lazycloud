package snapshotter

import (
	"context"
	"fmt"
	"net/url"
	"sync"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
)

// maxGrantsPerCall bounds one Grant call; an image has at most a few hundred
// layers.
const maxGrantsPerCall = 4096

// grant is where one layer's index and data objects are read until expires.
type grant struct {
	indexURL string
	dataURL  string
	expires  time.Time
}

// grants holds the newest grant of each layer the agent granted. It is
// memory only: after a restart reads fail until the agent grants again,
// which it does before it starts a container and before grants expire.
type grants struct {
	now func() time.Time

	mu      sync.Mutex
	byLayer map[imagefs.Digest]grant
}

func newGrants(now func() time.Time) *grants {
	return &grants{now: now, byLayer: make(map[imagefs.Digest]grant)}
}

// lookup returns layer's grant if it has not expired.
func (g *grants) lookup(layer imagefs.Digest) (grant, bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	current, ok := g.byLayer[layer]
	if !ok || !g.now().Before(current.expires) {
		return grant{}, false
	}
	return current, true
}

// put records each grant unless the layer's current one expires later, and
// drops expired grants.
func (g *grants) put(layers map[imagefs.Digest]grant) {
	g.mu.Lock()
	defer g.mu.Unlock()
	now := g.now()
	for layer, current := range g.byLayer {
		if !now.Before(current.expires) {
			delete(g.byLayer, layer)
		}
	}
	for layer, next := range layers {
		if current, ok := g.byLayer[layer]; ok && current.expires.After(next.expires) {
			continue
		}
		g.byLayer[layer] = next
	}
}

// layerSources serves LayerSources on the snapshotter's socket.
type layerSources struct {
	imagefsproto.UnimplementedLayerSourcesServer
	grants *grants
	frames *frameCache
}

// Grant validates every grant before it records any, so a refused call
// changes nothing.
func (s layerSources) Grant(_ context.Context, request *imagefsproto.GrantRequest) (*imagefsproto.GrantResponse, error) {
	if len(request.GetLayers()) > maxGrantsPerCall {
		return nil, status.Errorf(codes.InvalidArgument, "a call grants at most %d layers", maxGrantsPerCall)
	}
	layers := make(map[imagefs.Digest]grant, len(request.GetLayers()))
	for _, l := range request.GetLayers() {
		layer := imagefs.Digest(l.GetDiffId())
		if err := layer.Check(); err != nil {
			return nil, status.Error(codes.InvalidArgument, err.Error())
		}
		for name, raw := range map[string]string{"index_url": l.GetIndexUrl(), "data_url": l.GetDataUrl()} {
			if err := checkURL(raw); err != nil {
				return nil, status.Errorf(codes.InvalidArgument, "layer %s %s: %v", layer, name, err)
			}
		}
		if l.GetExpiresAt() == nil {
			return nil, status.Errorf(codes.InvalidArgument, "layer %s has no expires_at", layer)
		}
		next := grant{indexURL: l.GetIndexUrl(), dataURL: l.GetDataUrl(), expires: l.GetExpiresAt().AsTime()}
		if current, ok := layers[layer]; ok && current.expires.After(next.expires) {
			continue
		}
		layers[layer] = next
	}
	s.grants.put(layers)
	return &imagefsproto.GrantResponse{}, nil
}

func checkURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("unparsable URL")
	}
	if (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" {
		return fmt.Errorf("not an absolute HTTP URL")
	}
	return nil
}
