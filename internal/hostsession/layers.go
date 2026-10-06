package hostsession

import (
	"cmp"
	"context"
	"fmt"
	"maps"
	"slices"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
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
	grants []*imagefsproto.LayerGrant
	grant  layerGrant
	err    error
}

// signLayers signs reads of every layer of each of references for the
// host in one call, skipping those the sync signed already. A reference
// left out has no converted layers. Layers its region's copy of the layer
// bucket is not confirmed to hold read from the layer bucket, and a check
// of that copy starts.
//
// Confirmations are watched from before the read, so none committed after
// it is missed: in the host's region once a read named it, and until then
// on every key, narrowed after the read. A wake-up the wider watch held
// makes the grants just issued due.
func (sess *session) signLayers(ctx context.Context, cache *syncCache, references []string) {
	references = slices.DeleteFunc(slices.Clone(references), func(r string) bool { _, ok := cache.layers[r]; return ok })
	if len(references) == 0 {
		return
	}
	slices.Sort(references)
	references = slices.Compact(references)
	watched := "" // every key
	if sess.region != "" {
		watched = images.ReplicasConfirmed(sess.region)
	}
	sess.replicas.set(sess.server.listener, watched)
	issued, lifetime := time.Now(), sess.server.config.LayerLifetime
	reads, err := sess.server.images.LayerReadURLsOf(ctx, references, sess.host, lifetime)
	var unconfirmed []string
	for _, reference := range references {
		r, ok := reads[reference]
		if err != nil || !ok {
			cache.layers[reference] = issuedLayers{err: cmp.Or(err, fmt.Errorf("%s: %w", reference, images.ErrNotConverted))}
			continue
		}
		first := issued.Add(lifetime)
		l := issuedLayers{grants: make([]*imagefsproto.LayerGrant, len(r.Layers))}
		for n, u := range r.Layers {
			l.grants[n] = &imagefsproto.LayerGrant{DiffId: string(u.DiffID), IndexUrl: u.Index, DataUrl: u.Data, ExpiresAt: timestamppb.New(u.ExpiresAt)}
			if u.ExpiresAt.Before(first) {
				first = u.ExpiresAt
			}
		}
		l.grant = layerGrant{renewAt: issued.Add(first.Sub(issued) / 3), unconfirmed: r.Unconfirmed}
		if r.Unconfirmed != "" {
			sess.region = r.Unconfirmed
			unconfirmed = append(unconfirmed, reference)
		}
		cache.layers[reference] = l
	}
	var woken bool
	if sess.region != "" {
		woken = sess.replicas.set(sess.server.listener, images.ReplicasConfirmed(sess.region))
	} else {
		sess.replicas.set(sess.server.listener)
	}
	for _, reference := range unconfirmed {
		if woken {
			l := cache.layers[reference]
			l.grant.renewAt = time.Time{}
			cache.layers[reference] = l
		}
		sess.server.confirmReplicas(reference, sess.region, telemetry.TraceParentOf(ctx)) //nolint:contextcheck // Checks run under the server's lifetime.
	}
}

// grantLayers returns the grants of reference, signing them unless the sync
// signed them already.
func (sess *session) grantLayers(ctx context.Context, cache *syncCache, reference string) ([]*imagefsproto.LayerGrant, layerGrant, error) {
	sess.signLayers(ctx, cache, []string{reference})
	l := cache.layers[reference]
	return l.grants, l.grant, l.err
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
	ctx, span := telemetry.Start(ctx, "hostsession.refresh_grants", trace.WithAttributes(telemetry.Host(sess.host.String())))
	defer span.End()
	references, err := sess.server.execution.LiveImagesOnHost(ctx, sess.host)
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
	sess.layersListed = true
	live := make(map[string]bool, len(references))
	var due []string
	for _, image := range references {
		live[image] = true
		if g, ok := sess.layers[image]; !ok || g.due(now) {
			due = append(due, image)
		}
	}
	sess.signLayers(ctx, cache, due)
	for _, image := range due {
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
// already; only those need a sync.
func (sess *session) outdateUnconfirmed() bool {
	now, changed := time.Now(), false
	for _, grants := range []map[string]layerGrant{sess.layers, sess.platform.answers} {
		for key, g := range grants {
			if g.unconfirmed != "" && !g.due(now) {
				grants[key] = layerGrant{unconfirmed: g.unconfirmed}
				changed = true
			}
		}
	}
	return changed
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
