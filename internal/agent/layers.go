package agent

import (
	"context"
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
