package snapshotter

import (
	"context"
	"net/url"
	"sync"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
)

// maxGrantsPerCall bounds one call's layers; an image has at most a few
// hundred.
const maxGrantsPerCall = 4096

// grant is where one layer's index and data objects are read until expires.
type grant struct {
	layer    imagefs.Digest
	indexURL string
	dataURL  string
	expires  time.Time
}

// grants holds the newest grant of each layer the agent granted. It is
// memory only: after a restart reads fail until the agent grants again,
// which it does before it starts a container and before grants expire.
type grants struct {
	mu      sync.Mutex
	byLayer map[imagefs.Digest]grant
}

// lookup returns layer's grant if it has not expired.
func (g *grants) lookup(layer imagefs.Digest) (grant, bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	current, ok := g.byLayer[layer]
	if !ok || !time.Now().Before(current.expires) {
		return grant{}, false
	}
	return current, true
}

// put records each grant unless its layer's grant expires later, and drops
// expired grants.
func (g *grants) put(next []grant) {
	g.mu.Lock()
	defer g.mu.Unlock()
	now := time.Now()
	for layer, current := range g.byLayer {
		if !now.Before(current.expires) {
			delete(g.byLayer, layer)
		}
	}
	for _, n := range next {
		if current, ok := g.byLayer[n.layer]; !ok || !current.expires.After(n.expires) {
			g.byLayer[n.layer] = n
		}
	}
}

// layerSources serves LayerSources on the snapshotter's socket.
type layerSources struct {
	imagefsproto.UnimplementedLayerSourcesServer
	cache *frameCache
}

// Grant validates every grant before it records any, so a refused call
// changes nothing.
func (s layerSources) Grant(ctx context.Context, request *imagefsproto.GrantRequest) (*imagefsproto.GrantResponse, error) {
	ids := make([]string, len(request.GetLayers()))
	for i, l := range request.GetLayers() {
		ids[i] = l.GetDiffId()
	}
	layers, err := digestsIn(ids)
	if err != nil {
		return nil, err
	}
	next := make([]grant, len(layers))
	for i, l := range request.GetLayers() {
		next[i] = grant{layer: layers[i], indexURL: l.GetIndexUrl(), dataURL: l.GetDataUrl(), expires: l.GetExpiresAt().AsTime()}
		if !httpURL(next[i].indexURL) || !httpURL(next[i].dataURL) || l.GetExpiresAt() == nil {
			return nil, status.Errorf(codes.InvalidArgument, "layer %s needs absolute HTTP index and data URLs and an expiry", layers[i])
		}
	}
	s.cache.grants.put(next)
	// A refresh names no start and brings no new reader: every start grants
	// its layers under its name first, and a layer already mounted when a
	// trace begins leaves that trace incomplete.
	if request.GetName() != "" {
		s.cache.traces.claim(request.GetName(), layers)
	}
	s.cache.starts.begin(request.GetName(), incomingParent(ctx), layers)
	return &imagefsproto.GrantResponse{}, nil
}

func httpURL(raw string) bool {
	u, err := url.Parse(raw)
	return err == nil && (u.Scheme == "https" || u.Scheme == "http") && u.Host != ""
}

func (s layerSources) Prefetch(ctx context.Context, request *imagefsproto.PrefetchRequest) (*imagefsproto.PrefetchResponse, error) {
	layers, err := namedLayers(request.GetName(), request.GetLayers())
	if err != nil {
		return nil, err
	}
	if len(request.GetReads()) > imagefs.MaxTraceReads {
		return nil, status.Errorf(codes.InvalidArgument, "a prefetch names at most %d frames", imagefs.MaxTraceReads)
	}
	reads := make([]frameKey, len(request.GetReads()))
	for i, r := range request.GetReads() {
		if int(r.GetLayer()) >= len(layers) {
			return nil, status.Errorf(codes.InvalidArgument, "read %d names layer %d of %d", i, r.GetLayer(), len(layers))
		}
		reads[i] = frameKey{layer: layers[r.GetLayer()], frame: int(r.GetFrame())}
	}
	s.cache.traces.claim(request.GetName(), layers)
	//nolint:contextcheck // a prefetch lives with the cache, not the call
	return &imagefsproto.PrefetchResponse{}, s.cache.prefetch(request.GetName(), reads, incomingParent(ctx))
}

func (s layerSources) StopPrefetch(_ context.Context, request *imagefsproto.StopPrefetchRequest) (*imagefsproto.StopPrefetchResponse, error) {
	s.cache.stopPrefetch(request.GetName())
	return &imagefsproto.StopPrefetchResponse{}, nil
}

func (s layerSources) StartTrace(_ context.Context, request *imagefsproto.StartTraceRequest) (*imagefsproto.StartTraceResponse, error) {
	layers, err := namedLayers(request.GetName(), request.GetLayers())
	if err != nil {
		return nil, err
	}
	return &imagefsproto.StartTraceResponse{}, s.cache.startTrace(request.GetName(), layers)
}

func (s layerSources) EndTrace(_ context.Context, request *imagefsproto.EndTraceRequest) (*imagefsproto.EndTraceResponse, error) {
	return s.cache.traces.end(request.GetName())
}

// namedLayers checks the start name and the diff_ids of a call.
func namedLayers(name string, layers []string) ([]imagefs.Digest, error) {
	if name == "" || len(name) > maxName {
		return nil, status.Errorf(codes.InvalidArgument, "a name has 1 to %d bytes", maxName)
	}
	return digestsIn(layers)
}

// digestsIn checks and converts the diff_ids of a call.
func digestsIn(layers []string) ([]imagefs.Digest, error) {
	if len(layers) > maxGrantsPerCall {
		return nil, status.Errorf(codes.InvalidArgument, "a call names at most %d layers", maxGrantsPerCall)
	}
	out := make([]imagefs.Digest, len(layers))
	for i, raw := range layers {
		out[i] = imagefs.Digest(raw)
		if err := out[i].Check(); err != nil {
			return nil, status.Error(codes.InvalidArgument, err.Error())
		}
	}
	return out, nil
}
