package agent

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

const (
	// grantTimeout bounds one call to the snapshotter, which answers from
	// memory.
	grantTimeout = 10 * time.Second
	// Failed refreshes are retried after a delay doubling up to
	// maxGrantRetry.
	minGrantRetry = time.Second
	maxGrantRetry = 30 * time.Second
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
}

func newLayerSources(client *layersource.Client) *layerSources {
	return &layerSources{client: client, wake: make(chan struct{}, 1), pending: map[imagefs.Digest]layersource.Grant{}}
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

// grant hands layers to the snapshotter and returns once it holds them. A
// host without a snapshotter fails its preflight check before it runs
// anything, so l is nil only on hosts that never get layers.
func (l *layerSources) grant(ctx context.Context, layers []*hostproto.LayerGrant) error {
	if len(layers) == 0 || l == nil {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, grantTimeout)
	defer cancel()
	return l.client.Grant(ctx, grantsIn(layers)) //nolint:wrapcheck // The client names the call.
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
			err := l.client.Grant(call, grants)
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

// prefetch has the snapshotter fetch the frames earlier containers of the
// start's image read before they were ready. Prefetching only speeds the
// start up, so a refusal is logged.
func (l *layerSources) prefetch(ctx context.Context, log *slog.Logger, spec *hostproto.StartContainer) {
	trace := spec.GetPrefetch().GetReads()
	if l == nil || len(trace) == 0 {
		return
	}
	reads := make([]layersource.FrameRead, len(trace))
	for n, r := range trace {
		reads[n] = layersource.FrameRead{Layer: r.GetLayer(), Frame: r.GetFrame()}
	}
	ctx, cancel := context.WithTimeout(ctx, grantTimeout)
	defer cancel()
	if err := l.client.Prefetch(ctx, layersOf(spec.GetLayers()), reads); err != nil {
		log.Warn("prefetching the image failed", "error", err)
	}
}

// startTrace has the snapshotter record the frames the container reads
// until endTrace, and reports whether it does.
func (l *layerSources) startTrace(ctx context.Context, log *slog.Logger, container string, spec *hostproto.StartContainer) bool {
	if l == nil || !spec.GetRecordTrace() || len(spec.GetLayers()) == 0 {
		return false
	}
	ctx, cancel := context.WithTimeout(ctx, grantTimeout)
	defer cancel()
	if err := l.client.StartTrace(ctx, container, layersOf(spec.GetLayers())); err != nil {
		log.Warn("tracing the image's startup reads failed", "error", err)
		return false
	}
	return true
}

// endTrace stops the container's trace and, when report is set and every
// read reached it, reports it to the server.
func (l *layerSources) endTrace(ctx context.Context, a *Agent, log *slog.Logger, container string, report bool) {
	ctx, cancel := context.WithTimeout(ctx, grantTimeout)
	defer cancel()
	reads, complete, err := l.client.EndTrace(ctx, container)
	if err != nil {
		log.Warn("ending the startup read trace failed", "error", err)
		return
	}
	if !report || !complete || len(reads) == 0 {
		return
	}
	trace := &hostproto.ImageTrace{Reads: make([]*hostproto.FrameRead, len(reads))}
	for n, r := range reads {
		trace.Reads[n] = &hostproto.FrameRead{Layer: r.Layer, Frame: r.Frame}
	}
	a.report(&hostproto.HostMessage{Body: &hostproto.HostMessage_StartupTrace{StartupTrace: &hostproto.StartupTrace{ContainerId: container, Trace: trace}}})
}
