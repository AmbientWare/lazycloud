package hostsession

import (
	"context"
	"maps"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// LayerLifetime is how long the URLs of a layer grant last: the hour other
// host transfers get, which the signing credentials may shorten to no less
// than about 14 minutes. A grant is renewed once a third of its life has
// passed, so a host keeps reading through 40 minutes without the control
// plane, and a reconnected session sends every grant again at once. A host
// that ran an image may read its layers that long after its last container
// of it stops.
const LayerLifetime = time.Hour

// layerGrant is when the grants sent for one image are renewed: a third of
// the way through the life of the first to expire, or at once when they
// are outdated or could not be issued. unconfirmed is the host's region
// when some of them read the layer bucket because the region's copy was not
// confirmed to hold them.
type layerGrant struct {
	renewAt     time.Time
	unconfirmed string
}

func (g layerGrant) due(now time.Time) bool { return !now.Before(g.renewAt) }

// issuedLayers is one signing of an image's layers, or why it failed.
type issuedLayers struct {
	grants []*hostproto.LayerGrant
	grant  layerGrant
	err    error
}

// grantLayers signs reads of every layer of reference for the host, once
// per sync. Layers its region's copy of the layer bucket is not confirmed to
// hold read from the layer bucket; the session then watches that region's
// confirmations before a check of the copy starts, so none the check
// records is missed. One another server records between the read and the
// watch reaches the host at the grant's renewal.
func (sess *session) grantLayers(ctx context.Context, cache *syncCache, reference string) ([]*hostproto.LayerGrant, layerGrant, error) {
	if l, ok := cache.layers[reference]; ok {
		return l.grants, l.grant, l.err
	}
	issued := time.Now()
	reads, err := sess.server.images.LayerReadURLs(ctx, reference, sess.host, sess.server.config.LayerLifetime)
	l := issuedLayers{err: err}
	if err == nil {
		first := issued.Add(sess.server.config.LayerLifetime)
		l.grants = make([]*hostproto.LayerGrant, len(reads.Layers))
		for n, u := range reads.Layers {
			l.grants[n] = &hostproto.LayerGrant{DiffId: string(u.DiffID), IndexUrl: u.Index, DataUrl: u.Data, ExpiresAt: timestamppb.New(u.ExpiresAt)}
			if u.ExpiresAt.Before(first) {
				first = u.ExpiresAt
			}
		}
		l.grant = layerGrant{renewAt: issued.Add(first.Sub(issued) / 3), unconfirmed: reads.Unconfirmed}
		if reads.Unconfirmed != "" {
			sess.replicas.set(sess.server.listener, images.ReplicasConfirmed(reads.Unconfirmed))
			sess.server.confirmReplicas(reference, reads.Unconfirmed, telemetry.TraceParentOf(ctx)) //nolint:contextcheck // Checks run under the server's lifetime.
		}
	}
	cache.layers[reference] = l
	return l.grants, l.grant, l.err //nolint:wrapcheck // permanentStartFailure matches the owner's error.
}

// grantFailed logs grants that could not be issued, which the next sync
// tries again while the host keeps its earlier ones until they expire, or
// ends the session when its context ended.
func (sess *session) grantFailed(ctx context.Context, image string, err error) error {
	if ctx.Err() != nil {
		return sess.server.grpcError(ctx, ctx.Err())
	}
	sess.server.logger.WarnContext(ctx, "issuing layer grants failed", "host", sess.host.String(), "image", image, "error", err)
	return nil
}

// refreshLayers keeps fresh layer grants on the host for every image its
// live containers run, and forgets the others. It lists them when the
// session opens and when a grant it sent is due, not on every sync.
func (sess *session) refreshLayers(ctx context.Context, cache *syncCache) error {
	now := time.Now()
	if sess.layersListed && !anyDue(sess.layers, now) {
		return nil
	}
	ctx, span := telemetry.StartIn(ctx, sess.server.tracer, "", "hostsession.refresh_grants", trace.WithAttributes(telemetry.Host(sess.host.String())))
	defer span.End()
	references, err := sess.server.execution.LiveImagesOnHost(ctx, sess.host)
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
	sess.layersListed = true
	live := make(map[string]bool, len(references))
	for _, image := range references {
		live[image] = true
		if g, ok := sess.layers[image]; ok && !g.due(now) {
			continue
		}
		layers, grant, err := sess.grantLayers(ctx, cache, image)
		if err != nil {
			if err := sess.grantFailed(ctx, image, err); err != nil {
				return err
			}
			sess.layers[image] = layerGrant{}
			continue
		}
		msg := &hostproto.ServerMessage{
			CommandId: "layers:" + image + ":" + now.Format(time.RFC3339Nano),
			Body:      &hostproto.ServerMessage_LayerGrants{LayerGrants: &hostproto.LayerGrants{Layers: layers}},
		}
		if err := sess.stream.Send(msg); err != nil {
			return err //nolint:wrapcheck // The stream's status ends the session.
		}
		sess.layers[image] = grant
	}
	maps.DeleteFunc(sess.layers, func(image string, _ layerGrant) bool { return !live[image] })
	return nil
}

func anyDue(grants map[string]layerGrant, now time.Time) bool {
	for _, g := range grants {
		if g.due(now) {
			return true
		}
	}
	return false
}

// outdateUnconfirmed makes every grant that waits for a regional copy due,
// after a confirmation in that region. It reports whether any was not due
// already, which only a sync renews.
func (sess *session) outdateUnconfirmed() bool {
	now, changed := time.Now(), false
	for _, grants := range []map[string]layerGrant{sess.layers, sess.platform.sent} {
		for key, g := range grants {
			if g.unconfirmed != "" && !g.due(now) {
				grants[key] = layerGrant{unconfirmed: g.unconfirmed}
				changed = true
			}
		}
	}
	return changed
}

// unwatchConfirmed ends the watch of confirmations once no grant waits for a
// regional copy.
func (sess *session) unwatchConfirmed() {
	for _, grants := range []map[string]layerGrant{sess.layers, sess.platform.sent} {
		for _, g := range grants {
			if g.unconfirmed != "" {
				return
			}
		}
	}
	sess.replicas.set(sess.server.listener)
}

const (
	// maxReplicaChecks bounds the checks of layer copies one server runs at
	// once. Each layer and region is checked once across the replicas, so
	// this bounds only how fast new images reach new regions.
	maxReplicaChecks = 4
	// ReplicaRecheck is how often a check looks again for layers its
	// region's copy did not hold yet, and how long a claim of one holds off
	// other servers.
	ReplicaRecheck = time.Minute
	// replicationWindow is how long a check keeps looking: Replication Time
	// Control copies nearly every object within 15 minutes. A layer still
	// missing after it is checked again when a grant next reads it.
	replicationWindow = 15 * time.Minute
)

// replicaChecks runs the checks of regional layer copies grants ask for,
// one per reference and region, under the server's lifetime.
type replicaChecks struct {
	mu      sync.Mutex
	running map[string]bool
	wg      sync.WaitGroup
}

// confirmReplicas starts checking region's copy for the layers of
// reference unless this server already is, or runs maxReplicaChecks. The
// check looks again every ReplicaRecheck while a layer is missing, for up
// to replicationWindow; each confirmation it records wakes the sessions
// whose grants it outdates. A reference it skips is asked for again by its
// next grant.
func (s *Server) confirmReplicas(reference, region, traceparent string) {
	key := reference + " " + region
	s.replicas.mu.Lock()
	defer s.replicas.mu.Unlock()
	if s.replicas.running[key] || len(s.replicas.running) >= maxReplicaChecks {
		return
	}
	s.replicas.running[key] = true
	s.replicas.wg.Go(func() {
		defer func() {
			s.replicas.mu.Lock()
			delete(s.replicas.running, key)
			s.replicas.mu.Unlock()
		}()
		ctx, span := telemetry.StartIn(s.lifetime, s.tracer, traceparent, "images.replica_checks", trace.WithAttributes(
			attribute.String(telemetry.AttrImage, reference), attribute.String("lazycloud.region", region)))
		defer span.End()
		recheck := s.config.ReplicaRecheck
		until := time.Now().Add(replicationWindow)
		for {
			check, err := s.images.ConfirmReplicas(ctx, reference, region, recheck)
			if s.lifetime.Err() != nil {
				return
			}
			if err != nil {
				s.logger.WarnContext(s.lifetime, "checking regional layer copies failed", "image", reference, "region", region, "error", err)
			}
			if !check.Pending || !time.Now().Add(recheck).Before(until) {
				return
			}
			select {
			case <-s.lifetime.Done():
				return
			case <-time.After(recheck):
			}
		}
	})
}
