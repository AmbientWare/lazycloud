package snapshotter

import (
	"context"
	"errors"
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
	if len(request.GetLayers()) > maxGrantsPerCall {
		return nil, status.Errorf(codes.InvalidArgument, "a call grants at most %d layers", maxGrantsPerCall)
	}
	next := make([]grant, len(request.GetLayers()))
	layers := make([]imagefs.Digest, len(request.GetLayers()))
	for i, l := range request.GetLayers() {
		g := grant{layer: imagefs.Digest(l.GetDiffId()), indexURL: l.GetIndexUrl(), dataURL: l.GetDataUrl()}
		if err := g.layer.Check(); err != nil {
			return nil, status.Error(codes.InvalidArgument, err.Error())
		}
		if err := checkURL(g.indexURL); err != nil {
			return nil, status.Errorf(codes.InvalidArgument, "layer %s index_url: %v", g.layer, err)
		}
		if err := checkURL(g.dataURL); err != nil {
			return nil, status.Errorf(codes.InvalidArgument, "layer %s data_url: %v", g.layer, err)
		}
		if l.GetExpiresAt() == nil {
			return nil, status.Errorf(codes.InvalidArgument, "layer %s has no expires_at", g.layer)
		}
		g.expires = l.GetExpiresAt().AsTime()
		next[i], layers[i] = g, g.layer
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

func checkURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return errors.New("unparsable URL")
	}
	if (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" {
		return errors.New("not an absolute HTTP URL")
	}
	return nil
}

func (s layerSources) Prefetch(ctx context.Context, request *imagefsproto.PrefetchRequest) (*imagefsproto.PrefetchResponse, error) {
	if err := checkName(request.GetName()); err != nil {
		return nil, err
	}
	layers, err := digestsIn(request.GetLayers())
	if err != nil {
		return nil, err
	}
	if len(request.GetReads()) > maxTraceReads {
		return nil, status.Errorf(codes.InvalidArgument, "a prefetch names at most %d frames", maxTraceReads)
	}
	reads := make([]frameKey, len(request.GetReads()))
	for i, r := range request.GetReads() {
		if int(r.GetLayer()) >= len(layers) {
			return nil, status.Errorf(codes.InvalidArgument, "read %d names layer %d of %d", i, r.GetLayer(), len(layers))
		}
		reads[i] = frameKey{layer: layers[r.GetLayer()], frame: int(r.GetFrame())}
	}
	s.cache.traces.claim(request.GetName(), layers)
	if err := s.cache.prefetch(request.GetName(), reads, incomingParent(ctx)); err != nil { //nolint:contextcheck // a prefetch lives with the cache, not the call
		return nil, err
	}
	return &imagefsproto.PrefetchResponse{}, nil
}

func (s layerSources) StopPrefetch(_ context.Context, request *imagefsproto.StopPrefetchRequest) (*imagefsproto.StopPrefetchResponse, error) {
	s.cache.stopPrefetch(request.GetName())
	return &imagefsproto.StopPrefetchResponse{}, nil
}

func (s layerSources) StartTrace(_ context.Context, request *imagefsproto.StartTraceRequest) (*imagefsproto.StartTraceResponse, error) {
	if err := checkName(request.GetName()); err != nil {
		return nil, err
	}
	layers, err := digestsIn(request.GetLayers())
	if err != nil {
		return nil, err
	}
	if err := s.cache.startTrace(request.GetName(), layers); err != nil {
		return nil, err
	}
	return &imagefsproto.StartTraceResponse{}, nil
}

func (s layerSources) EndTrace(_ context.Context, request *imagefsproto.EndTraceRequest) (*imagefsproto.EndTraceResponse, error) {
	tr, ok := s.cache.traces.end(request.GetName())
	if !ok {
		return nil, status.Errorf(codes.NotFound, "no trace %q is recording", request.GetName())
	}
	out := &imagefsproto.EndTraceResponse{Complete: tr.complete, Reads: make([]*imagefsproto.FrameRead, len(tr.reads))}
	for i, r := range tr.reads {
		out.Reads[i] = &imagefsproto.FrameRead{Layer: r.layer, Frame: r.frame}
	}
	return out, nil
}

// digestsIn checks and converts the diff_ids of a request.
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

func checkName(name string) error {
	if name == "" || len(name) > maxName {
		return status.Errorf(codes.InvalidArgument, "a name has 1 to %d bytes", maxName)
	}
	return nil
}
