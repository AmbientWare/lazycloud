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
	// container is ready. A recording start has no prefetch, and a first
	// task importing torch from the store then ends about 11 s after the
	// trace began on an m7i.large.
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

// grant hands layers to the snapshotter for the start name and returns
// once it holds them; l is nil only on hosts that never get layers.
func (l *layerSources) grant(ctx context.Context, name string, layers []*hostproto.LayerGrant) error {
	if l == nil || len(layers) == 0 {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, grantTimeout)
	defer cancel()
	return l.client.Grant(ctx, name, grantsIn(layers)) //nolint:wrapcheck // The client names the call.
}

// refresh queues fresh grants for refreshLoop.
func (l *layerSources) refresh(layers []*hostproto.LayerGrant) {
	if l == nil {
		return
	}
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
func (l *layerSources) refreshLoop(ctx context.Context, a *Agent) {
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
			a.log.Error("refreshing layer grants failed; retrying", "layers", len(grants), "retry_in", retry, "error", err)
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
	// shared is set once another start on the host used a traced layer:
	// the snapshotter cannot tell their reads apart.
	shared bool
	// traced is when the trace began. over closes once the container's
	// first task or request ended or the container exited.
	traced time.Time
	over   chan struct{}
	ended  bool
}

// end closes over once.
func (s *startup) end() {
	if !s.ended {
		s.ended = true
		close(s.over)
	}
}

// shares reports whether s uses one of layers.
func (s *startup) shares(layers []imagefs.Digest) bool {
	return slices.ContainsFunc(layers, func(l imagefs.Digest) bool { return slices.Contains(s.layers, l) })
}

// begin records that container starts with layers, marks the traces of
// other starts sharing one as shared, and reports whether one shares.
func (l *layerSources) begin(container string, layers []imagefs.Digest) bool {
	l.smu.Lock()
	defer l.smu.Unlock()
	shared := false
	for id, other := range l.startups {
		if id != container && other.shares(layers) {
			other.shared = true
			shared = true
		}
	}
	l.startups[container] = &startup{layers: layers, over: make(chan struct{})}
	return shared
}

// start hands the start's grants to the snapshotter, which then reads the
// image's layers only through them, and returns once it holds them. It
// then starts the start's prefetch and its trace when asked and no other
// start on the host shares a layer, and reports whether it traces.
// Prefetching and tracing only speed starts up, so their refusals are
// logged. A host without a snapshotter fails its preflight check before it
// runs anything, so l is nil only on hosts that never get layers.
func (l *layerSources) start(ctx context.Context, log *slog.Logger, container string, spec *hostproto.StartContainer) (bool, error) {
	if l == nil || len(spec.GetLayers()) == 0 {
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
	if trace := spec.GetPrefetch().GetReads(); len(trace) > 0 {
		reads := make([]layersource.FrameRead, len(trace))
		for n, r := range trace {
			reads[n] = layersource.FrameRead{Layer: r.GetLayer(), Frame: r.GetFrame()}
		}
		if err := l.client.Prefetch(ctx, container, layers, reads); err != nil {
			log.Warn("prefetching the image failed", "error", err)
		} else {
			l.mark(container, func(s *startup) { s.prefetching = true })
		}
	}
	if !spec.GetRecordTrace() || shared {
		return false, nil
	}
	traced := time.Now()
	if err := l.client.StartTrace(ctx, container, layers); err != nil {
		log.Warn("tracing the image's startup reads failed", "error", err)
		return false, nil
	}
	l.mark(container, func(s *startup) { s.tracing, s.traced = true, traced })
	return true, nil
}

func (l *layerSources) mark(container string, change func(*startup)) {
	l.smu.Lock()
	defer l.smu.Unlock()
	if s, ok := l.startups[container]; ok {
		change(s)
	}
}

// takeTrace stops counting container's trace and reports whether it ran
// and whether another start shared a layer with it.
func (l *layerSources) takeTrace(container string) (tracing, shared bool) {
	if l == nil {
		return false, false
	}
	l.smu.Lock()
	defer l.smu.Unlock()
	s, ok := l.startups[container]
	if !ok || !s.tracing {
		return false, false
	}
	s.tracing = false
	return true, s.shared
}

// served ends container's trace at the end of its first task or request.
func (l *layerSources) served(container string) {
	if l == nil {
		return
	}
	l.smu.Lock()
	defer l.smu.Unlock()
	if s, ok := l.startups[container]; ok {
		s.end()
	}
}

// await waits for container's first task or request to end, at most window
// from the start of its trace, then ends the trace as end does. It returns
// nil at once if the container does not trace, exits or ctx ends.
func (l *layerSources) await(ctx context.Context, log *slog.Logger, container string, window time.Duration) *hostproto.ImageTrace {
	l.smu.Lock()
	s, ok := l.startups[container]
	if !ok || !s.tracing {
		l.smu.Unlock()
		return nil
	}
	traced, over := s.traced, s.over
	l.smu.Unlock()
	timer := time.NewTimer(time.Until(traced.Add(window)))
	defer timer.Stop()
	select {
	case <-over:
	case <-timer.C:
	case <-ctx.Done():
		return nil
	}
	return l.end(ctx, log, container)
}

// end ends container's trace and returns it when every read reached it and
// only this start used its layers, or nil.
func (l *layerSources) end(ctx context.Context, log *slog.Logger, container string) *hostproto.ImageTrace {
	tracing, shared := l.takeTrace(container)
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
	if s, ok := l.startups[container]; ok && s.shared {
		shared = true
	}
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
	if l == nil {
		return
	}
	l.smu.Lock()
	s, ok := l.startups[container]
	delete(l.startups, container)
	if ok {
		s.end()
	}
	l.smu.Unlock()
	if !ok || (!s.prefetching && !s.tracing) {
		return
	}
	ctx, cancel := context.WithTimeout(ctx, grantTimeout)
	defer cancel()
	if s.prefetching {
		if err := l.client.StopPrefetch(ctx, container); err != nil {
			log.Warn("stopping the prefetch failed", "error", err)
		}
	}
	if s.tracing {
		if _, _, err := l.client.EndTrace(ctx, container); err != nil {
			log.Warn("ending the startup read trace failed", "error", err)
		}
	}
}
