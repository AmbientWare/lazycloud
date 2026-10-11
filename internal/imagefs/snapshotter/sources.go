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

// layerURLs is where one layer's index and data objects are read.
type layerURLs struct{ index, data string }

// grants holds the newest grant of each layer or disk the agent granted
// until it expires. It is memory only: after a restart reads fail until the
// agent grants again, which it does before it starts a container or serves
// a disk and before grants expire.
type grants[K comparable, V any] struct {
	mu    sync.Mutex
	byKey map[K]expiring[V]
}

type expiring[V any] struct {
	value   V
	expires time.Time
}

// lookup returns k's grant if it has not expired.
func (g *grants[K, V]) lookup(k K) (V, bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	current, ok := g.byKey[k]
	if !ok || !time.Now().Before(current.expires) {
		var none V
		return none, false
	}
	return current.value, true
}

// put records the grant value makes for k until expires, unless k's grant
// expires later, and drops expired grants.
func (g *grants[K, V]) put(k K, expires time.Time, value func() V) {
	g.mu.Lock()
	defer g.mu.Unlock()
	now := time.Now()
	for key, current := range g.byKey {
		if !now.Before(current.expires) {
			delete(g.byKey, key)
		}
	}
	if current, ok := g.byKey[k]; !ok || !current.expires.After(expires) {
		if g.byKey == nil {
			g.byKey = map[K]expiring[V]{}
		}
		g.byKey[k] = expiring[V]{value: value(), expires: expires}
	}
}

// drop forgets k's grant.
func (g *grants[K, V]) drop(k K) {
	g.mu.Lock()
	defer g.mu.Unlock()
	delete(g.byKey, k)
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
	for i, l := range request.GetLayers() {
		if !httpURL(l.GetIndexUrl()) || !httpURL(l.GetDataUrl()) || l.GetExpiresAt() == nil {
			return nil, status.Errorf(codes.InvalidArgument, "layer %s needs absolute HTTP index and data URLs and an expiry", layers[i])
		}
	}
	for i, l := range request.GetLayers() {
		s.cache.grants.put(layers[i], l.GetExpiresAt().AsTime(), func() layerURLs { return layerURLs{index: l.GetIndexUrl(), data: l.GetDataUrl()} })
	}
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
		reads[i] = frameKey{object: string(layers[r.GetLayer()]), frame: int(r.GetFrame())}
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
