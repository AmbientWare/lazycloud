package agent

import (
	"context"
	"log/slog"
	"slices"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// grantTimeout bounds one call to the snapshotter, which answers from
	// memory.
	grantTimeout = 10 * time.Second
	// Failed refreshes are retried after a delay doubling up to
	// maxGrantRetry.
	minGrantRetry = time.Second
	maxGrantRetry = 30 * time.Second
	// traceWindow bounds a startup trace, which ends with the container's
	// first task or request: a handler's imports run in it, after the
	// container is ready, and a start that records its trace has no
	// prefetch to speed them.
	traceWindow = 60 * time.Second
)

// layerSources hands layer grants to the snapshotter. Refreshes go through
// one owned goroutine, so the session never waits on the snapshotter and at
// most one refresh call is in flight. A failed refresh is queued again and
// retried until it succeeds, a newer grant replaces it or it expires.
type layerSources struct {
	client *layersource.Client
	wake   chan struct{}

	mu sync.Mutex
	// pending holds the newest grant per layer not yet handed over.
	pending map[imagefs.Digest]layersource.Grant

	smu sync.Mutex
	// startups holds the containers started here that have not exited.
	startups map[string]*startup
}

func newLayerSources(client *layersource.Client) *layerSources {
	return &layerSources{
		client: client, wake: make(chan struct{}, 1),
		pending: map[imagefs.Digest]layersource.Grant{}, startups: map[string]*startup{},
	}
}

func grantsIn(layers []*hostproto.LayerGrant) []layersource.Grant {
	out := make([]layersource.Grant, len(layers))
	for n, g := range layers {
		out[n] = layersource.Grant{
			Layer: imagefs.Digest(g.GetDiffId()), IndexURL: g.GetIndexUrl(), DataURL: g.GetDataUrl(), ExpiresAt: g.GetExpiresAt().AsTime(),
		}
	}
	return out
}

// grant hands layers to the snapshotter for the platform image start name
// and returns once it holds them.
func (l *layerSources) grant(ctx context.Context, name string, layers []*hostproto.LayerGrant) error {
	if len(layers) == 0 {
		return nil
	}
	l.smu.Lock()
	l.shareLocked(name, layersOf(layers))
	l.smu.Unlock()
	ctx, cancel := context.WithTimeout(ctx, grantTimeout)
	defer cancel()
	return l.client.Grant(ctx, name, grantsIn(layers)) //nolint:wrapcheck // The client names the call.
}

// refresh queues fresh grants for refreshLoop.
func (l *layerSources) refresh(layers []*hostproto.LayerGrant) {
	l.queue(grantsIn(layers))
	select {
	case l.wake <- struct{}{}:
	default:
	}
}

// queue adds grants to pending, keeping the later of two grants for one
// layer and dropping expired ones.
func (l *layerSources) queue(grants []layersource.Grant) {
	now := time.Now()
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, g := range grants {
		if !g.ExpiresAt.After(now) {
			continue
		}
		if old, ok := l.pending[g.Layer]; !ok || g.ExpiresAt.After(old.ExpiresAt) {
			l.pending[g.Layer] = g
		}
	}
}

// take empties pending.
func (l *layerSources) take() []layersource.Grant {
	l.mu.Lock()
	defer l.mu.Unlock()
	grants := make([]layersource.Grant, 0, len(l.pending))
	for _, g := range l.pending {
		grants = append(grants, g)
	}
	clear(l.pending)
	return grants
}

// refreshLoop hands queued refreshes to the snapshotter until ctx ends.
func (l *layerSources) refreshLoop(ctx context.Context, log *slog.Logger) {
	retry := minGrantRetry
	for {
		select {
		case <-ctx.Done():
			return
		case <-l.wake:
		}
		for grants := l.take(); len(grants) > 0; grants = l.take() {
			call, cancel := context.WithTimeout(ctx, grantTimeout)
			err := l.client.Grant(call, "", grants)
			cancel()
			if err == nil {
				retry = minGrantRetry
				continue
			}
			if ctx.Err() != nil {
				return
			}
			log.Error("refreshing layer grants failed; retrying", "layers", len(grants), "retry_in", retry, "error", err)
			l.queue(grants)
			if !sleep(ctx, retry) {
				return
			}
			retry = min(2*retry, maxGrantRetry)
		}
	}
}

func layersOf(grants []*hostproto.LayerGrant) []imagefs.Digest {
	out := make([]imagefs.Digest, len(grants))
	for n, g := range grants {
		out[n] = imagefs.Digest(g.GetDiffId())
	}
	return out
}

// startup is what the snapshotter does for one container this agent
// started: the layers it uses, and whether it prefetches or traces them.
type startup struct {
	layers      []imagefs.Digest
	prefetching bool
	tracing     bool
	// began is when the start began; its trace runs at most traceWindow
	// from then.
	began time.Time
	// shared is set once another start or a platform image on the host
	// names one of its layers.
	shared bool
	// released is closed once the container exited or failed to start.
	released chan struct{}
}

// shareLocked marks shared every live start other than name that uses one
// of layers, and reports whether there was one.
func (l *layerSources) shareLocked(name string, layers []imagefs.Digest) bool {
	shared := false
	for id, other := range l.startups {
		if id != name && slices.ContainsFunc(layers, func(d imagefs.Digest) bool { return slices.Contains(other.layers, d) }) {
			other.shared, shared = true, true
		}
	}
	return shared
}

// begin records that container starts with layers and reports whether a
// live start on the host shares one of them.
func (l *layerSources) begin(container string, layers []imagefs.Digest) bool {
	l.smu.Lock()
	defer l.smu.Unlock()
	shared := l.shareLocked(container, layers)
	l.startups[container] = &startup{layers: layers, began: time.Now(), shared: shared, released: make(chan struct{})}
	return shared
}

// start hands the start's grants to the snapshotter, which then reads the
// image's layers only through them, and returns once it holds them. It
// then starts the start's prefetch, and its trace when asked and no live
// start on the host shares a layer, and reports whether it traces.
// Prefetching and tracing only speed starts up, so their refusals are
// logged.
//
// The snapshotter cannot tell apart the reads of starts sharing a layer. It
// leaves incomplete a trace that began with one of its layers mounted or
// whose layers a later grant names, but not one whose layers another start
// was granted, unmounted, before it began. The agent marks such a start
// shared, and end drops its trace.
func (l *layerSources) start(ctx context.Context, log *slog.Logger, container string, spec *hostproto.StartContainer) (bool, error) {
	if len(spec.GetLayers()) == 0 {
		return false, nil
	}
	layers := layersOf(spec.GetLayers())
	shared := l.begin(container, layers)
	ctx, span := telemetry.Start(ctx, "agent.layer_grant", trace.WithAttributes(attribute.Int("lazycloud.layers", len(layers)),
		attribute.Int("lazycloud.prefetch_frames", len(spec.GetPrefetch().GetReads())), attribute.Bool("lazycloud.shared", shared)))
	defer span.End()
	ctx, cancel := context.WithTimeout(ctx, grantTimeout)
	defer cancel()
	if err := l.client.Grant(ctx, container, grantsIn(spec.GetLayers())); err != nil {
		telemetry.Fail(span, err)
		return false, err //nolint:wrapcheck // The client names the call.
	}
	prefetching := false
	if trace := spec.GetPrefetch().GetReads(); len(trace) > 0 {
		reads := make([]layersource.FrameRead, len(trace))
		for n, r := range trace {
			reads[n] = layersource.FrameRead{Layer: r.GetLayer(), Frame: r.GetFrame()}
		}
		if err := l.client.Prefetch(ctx, container, layers, reads); err != nil {
			log.Warn("prefetching the image failed", "error", err)
		} else {
			prefetching = true
		}
	}
	tracing := spec.GetRecordTrace() && !shared
	if tracing {
		if err := l.client.StartTrace(ctx, container, layers); err != nil {
			log.Warn("tracing the image's startup reads failed", "error", err)
			tracing = false
		}
	}
	l.smu.Lock()
	if s, ok := l.startups[container]; ok {
		s.prefetching, s.tracing = prefetching, tracing
	}
	l.smu.Unlock()
	return tracing, nil
}

// await waits until served closes, at most traceWindow from container's
// start, then ends its trace as end does. It returns nil at
// once if the container does not trace, exits or ctx ends.
func (l *layerSources) await(ctx context.Context, log *slog.Logger, container string, served <-chan struct{}) *hostproto.ImageTrace {
	l.smu.Lock()
	s, ok := l.startups[container]
	if !ok || !s.tracing {
		l.smu.Unlock()
		return nil
	}
	began, released := s.began, s.released
	l.smu.Unlock()
	timer := time.NewTimer(time.Until(began.Add(traceWindow)))
	defer timer.Stop()
	select {
	case <-served:
	case <-timer.C:
	case <-released:
		return nil
	case <-ctx.Done():
		return nil
	}
	return l.end(ctx, log, container)
}

// end ends container's trace and returns it when the snapshotter counts
// every read in it as this start's, or nil.
func (l *layerSources) end(ctx context.Context, log *slog.Logger, container string) *hostproto.ImageTrace {
	l.smu.Lock()
	s, ok := l.startups[container]
	tracing := ok && s.tracing
	if tracing {
		s.tracing = false
	}
	l.smu.Unlock()
	if !tracing {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, grantTimeout)
	defer cancel()
	reads, complete, err := l.client.EndTrace(ctx, container)
	if err != nil {
		log.Warn("ending the startup read trace failed", "error", err)
		return nil
	}
	// A start that began while the trace ended shares it too.
	l.smu.Lock()
	shared := s.shared
	l.smu.Unlock()
	if !complete || shared || len(reads) == 0 {
		return nil
	}
	trace := &hostproto.ImageTrace{Reads: make([]*hostproto.FrameRead, len(reads))}
	for n, r := range reads {
		trace.Reads[n] = &hostproto.FrameRead{Layer: r.Layer, Frame: r.Frame}
	}
	return trace
}

// release forgets container once it exited or failed to start, and stops
// its prefetch and trace.
func (l *layerSources) release(ctx context.Context, log *slog.Logger, container string) {
	l.smu.Lock()
	s, ok := l.startups[container]
	delete(l.startups, container)
	prefetching, tracing := false, false
	if ok {
		close(s.released)
		prefetching, tracing = s.prefetching, s.tracing
	}
	l.smu.Unlock()
	if !prefetching && !tracing {
		return
	}
	ctx, cancel := context.WithTimeout(ctx, grantTimeout)
	defer cancel()
	if prefetching {
		if err := l.client.StopPrefetch(ctx, container); err != nil {
			log.Warn("stopping the prefetch failed", "error", err)
		}
	}
	if tracing {
		if _, _, err := l.client.EndTrace(ctx, container); err != nil {
			log.Warn("ending the startup read trace failed", "error", err)
		}
	}
}
