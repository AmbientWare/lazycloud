package edge

import (
	"context"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"golang.org/x/sync/errgroup"
)

// Wake-ups from migration 0006's triggers.
const (
	// channelRoute: a workload, app, route or custom domain changed.
	channelRoute = "lc_route"
	// channelEndpoint: a container of the workload in the payload changed
	// state.
	channelEndpoint = "lc_endpoint"
)

// routeDebounce gathers the route changes of one deploy into one reload.
const routeDebounce = 50 * time.Millisecond

// watch listens for route and container changes on its own connection,
// reconnecting after failures. Every (re)connect reloads routes and every
// tracked container set, because notifications sent while disconnected are
// lost.
func (e *Edge) watch(ctx context.Context) error {
	for {
		err := e.listenChanges(ctx)
		if ctx.Err() != nil {
			return nil //nolint:nilerr // Cancellation is the normal stop.
		}
		e.logger.WarnContext(ctx, "edge listener disconnected", "error", err)
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(time.Second):
		}
	}
}

func (e *Edge) listenChanges(ctx context.Context) error {
	pooled, err := e.pool.Acquire(ctx)
	if err != nil {
		return fmt.Errorf("acquire listener connection: %w", err)
	}
	// A listening connection must not return to the pool with LISTEN active.
	conn := pooled.Hijack()
	defer func() { _ = conn.Close(context.WithoutCancel(ctx)) }()
	for _, channel := range []string{channelRoute, channelEndpoint} {
		if _, err := conn.Exec(ctx, "listen "+pgx.Identifier{channel}.Sanitize()); err != nil {
			return fmt.Errorf("listen %s: %w", channel, err)
		}
	}
	if err := e.reloadRoutes(ctx); err != nil {
		return err
	}
	e.refreshAll()

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	notes := make(chan *pgconn.Notification)
	g, gctx := errgroup.WithContext(ctx)
	g.Go(func() error {
		for {
			n, err := conn.WaitForNotification(gctx)
			if err != nil {
				return fmt.Errorf("wait for notification: %w", err)
			}
			select {
			case notes <- n:
			case <-gctx.Done():
				return nil
			}
		}
	})
	g.Go(func() error {
		var debounce <-chan time.Time
		for {
			select {
			case <-gctx.Done():
				return nil
			case n := <-notes:
				switch n.Channel {
				case channelRoute:
					if debounce == nil {
						debounce = time.After(routeDebounce)
					}
				case channelEndpoint:
					if id, err := uuid.Parse(n.Payload); err == nil {
						e.markChanged(id)
					}
				}
			case <-debounce:
				debounce = nil
				if err := e.reloadRoutes(gctx); err != nil {
					return err
				}
			}
		}
	})
	if err := g.Wait(); err != nil {
		return fmt.Errorf("watch changes: %w", err)
	}
	return nil
}

func (e *Edge) reloadRoutes(ctx context.Context) error {
	e.reloading.Lock()
	defer e.reloading.Unlock()
	return e.reloadLocked(ctx)
}

func (e *Edge) reloadLocked(ctx context.Context) error {
	started := time.Now()
	routes, err := e.loadRoutes(ctx)
	if err != nil {
		return err
	}
	previous := e.routes.Swap(routes)
	e.reloadStarted = started
	e.forgetReplacedWindows(previous, routes)
	return nil
}

// forgetReplacedWindows clears the keep-warm window of releases a deploy
// replaced: the traffic it remembers now goes to the new release, and only
// pinned requests should keep the old one warm.
func (e *Edge) forgetReplacedWindows(previous, next *routeTable) {
	e.mu.Lock()
	defer e.mu.Unlock()
	for id, before := range previous.byID {
		after := next.byID[id]
		if before.active == nil || (after != nil && after.active != nil && after.active.id == before.active.id) {
			continue
		}
		if l := e.loads[before.active.id]; l != nil {
			l.window = windowMax{}
		}
	}
}

// reloadOnMiss reloads the route table for a request that arrived at since
// and found no route, unless a reload that began after it already finished.
// Reloads are serialized, so misses never run more than one at a time.
func (e *Edge) reloadOnMiss(ctx context.Context, since time.Time) error {
	e.reloading.Lock()
	defer e.reloading.Unlock()
	if e.reloadStarted.After(since) {
		return nil
	}
	return e.reloadLocked(ctx)
}
