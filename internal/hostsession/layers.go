package hostsession

import (
	"cmp"
	"context"
	"sync"
	"time"

	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/images"
)

// layerLifetime is how long the URLs of a layer grant last: the hour other
// host transfers get, which the signing credentials may shorten to no less
// than about 14 minutes. A grant is renewed once a third of its life has
// passed, so a host keeps reading through 40 minutes without the control
// plane, and a reconnected session sends every grant again at once. A host
// that ran an image may read its layers that long after its last container
// of it stops.
const layerLifetime = time.Hour

// layerGrant is when the grants for one image's layers were issued and when
// the first of them expires. unconfirmed is the host's region when some of
// them read the layer bucket because the region's copy was not confirmed to
// hold them, and stale marks grants a later confirmation there outdated.
type layerGrant struct {
	issued, expires time.Time
	unconfirmed     string
	stale           bool
}

// due reports whether the grant is stale or a third of its life has passed.
func (g layerGrant) due(now time.Time) bool {
	return g.stale || !now.Before(g.issued.Add(g.expires.Sub(g.issued)/3))
}

// issuedLayers are the grants issued for one reference and the region
// whose copy some of them wait for.
type issuedLayers struct {
	grants      []*hostproto.LayerGrant
	unconfirmed string
}

// layerCache holds the grants issued for each reference during one sync of
// one host, so the replicas of one image cost one query and one set of
// signatures.
type layerCache map[string]issuedLayers

// layers returns grants for every layer of reference on host. Layers its
// region's copy of the layer bucket is not confirmed to hold read from the
// layer bucket, and a check of that copy starts.
func (s *Server) layers(ctx context.Context, host compute.HostID, cache layerCache, reference string) ([]*hostproto.LayerGrant, error) {
	if issued, ok := cache[reference]; ok {
		return issued.grants, nil
	}
	reads, err := s.images.LayerReadURLs(ctx, reference, host, s.layerLifetime)
	if err != nil {
		return nil, err //nolint:wrapcheck // permanentStartFailure matches the owner's error.
	}
	if reads.Unconfirmed != "" {
		s.confirmReplicas(reference, reads.Unconfirmed)
	}
	out := make([]*hostproto.LayerGrant, len(reads.Layers))
	for n, u := range reads.Layers {
		out[n] = &hostproto.LayerGrant{
			DiffId: string(u.DiffID), IndexUrl: u.Index, DataUrl: u.Data, ExpiresAt: timestamppb.New(u.ExpiresAt),
		}
	}
	cache[reference] = issuedLayers{grants: out, unconfirmed: reads.Unconfirmed}
	return out, nil
}

const (
	// maxReplicaChecks bounds the checks of layer copies one server runs at
	// once. Each layer and region is checked once across the replicas, so
	// this bounds only how fast new images reach new regions.
	maxReplicaChecks = 4
	// replicaRecheck is how often a check looks again for layers its region's
	// copy did not hold yet, and how long a claim of one holds off other
	// servers.
	replicaRecheck = time.Minute
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
	// recheck is replicaRecheck; tests shorten it.
	recheck time.Duration
}

// confirmReplicas starts checking region's copy for the layers of
// reference unless this server already is, or runs maxReplicaChecks. The
// check looks again every recheck while a layer is missing, for up to
// replicationWindow; each confirmation it records wakes the sessions whose
// grants it outdates. A reference it skips is asked for again by its next
// grant.
func (s *Server) confirmReplicas(reference, region string) {
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
		until := time.Now().Add(replicationWindow)
		for {
			check, err := s.images.ConfirmReplicas(s.lifetime, reference, region, s.replicas.recheck)
			if s.lifetime.Err() != nil {
				return
			}
			if err != nil {
				s.logger.WarnContext(s.lifetime, "checking regional layer copies failed", "image", reference, "region", region, "error", err)
			}
			if !check.Pending || !time.Now().Add(s.replicas.recheck).Before(until) {
				return
			}
			select {
			case <-s.lifetime.Done():
				return
			case <-time.After(s.replicas.recheck):
			}
		}
	})
}

// watchReplicas subscribes the session to confirmations in its host's
// region once a grant waits for that region's copy. Grants issued before
// the subscription may have missed one, so they count as stale.
func (sess *session) watchReplicas() {
	if sess.replicaWake != nil {
		return
	}
	region := ""
	for _, g := range sess.layers {
		region = cmp.Or(region, g.unconfirmed)
	}
	for _, g := range sess.platform.sent {
		region = cmp.Or(region, g.unconfirmed)
	}
	if region == "" {
		return
	}
	sess.replicaWake, sess.replicaStop = sess.server.listener.Subscribe(database.ChannelImageBuild, images.ReplicasConfirmed(region))
	sess.staleUnconfirmed()
}

// staleUnconfirmed marks every grant that waits for a regional copy stale,
// so the next sync signs it again.
func (sess *session) staleUnconfirmed() {
	for _, grants := range []map[string]layerGrant{sess.layers, sess.platform.sent} {
		for key, g := range grants {
			if g.unconfirmed != "" {
				g.stale = true
				grants[key] = g
			}
		}
	}
}

// grantOf is the life of the grants cache holds for reference, issued at
// issued.
func (s *Server) grantOf(cache layerCache, reference string, issued time.Time) layerGrant {
	layers := cache[reference]
	g := layerGrant{issued: issued, expires: issued.Add(s.layerLifetime), unconfirmed: layers.unconfirmed}
	for _, l := range layers.grants {
		if at := l.GetExpiresAt().AsTime(); at.Before(g.expires) {
			g.expires = at
		}
	}
	return g
}

// refreshLayers keeps fresh layer grants on the host for every image its
// live containers run, and forgets the others. It lists them when the
// session opens and when a grant it sent is due, not on every sync.
// Failing to issue grants is logged and retried at the next sync: the host
// keeps its earlier ones until they expire.
func (sess *session) refreshLayers(ctx context.Context, cache layerCache) error {
	now := time.Now()
	if sess.layersListed {
		due := false
		for _, g := range sess.layers {
			due = due || g.due(now)
		}
		if !due {
			return nil
		}
	}
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
		layers, err := sess.server.layers(ctx, sess.host, cache, image)
		if err != nil {
			if ctx.Err() != nil {
				return status.FromContextError(ctx.Err()).Err()
			}
			sess.server.logger.WarnContext(ctx, "issuing layer grants failed", "host", sess.host.String(), "image", image, "error", err)
			continue
		}
		msg := &hostproto.ServerMessage{
			CommandId: "layers:" + image + ":" + now.Format(time.RFC3339Nano),
			Body:      &hostproto.ServerMessage_LayerGrants{LayerGrants: &hostproto.LayerGrants{Layers: layers}},
		}
		if err := sess.stream.Send(msg); err != nil {
			return err //nolint:wrapcheck // The stream's status ends the session.
		}
		sess.layers[image] = sess.server.grantOf(cache, image, now)
	}
	for image := range sess.layers {
		if !live[image] {
			delete(sess.layers, image)
		}
	}
	return nil
}
