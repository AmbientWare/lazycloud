package hostsession

import (
	"context"
	"sync"
	"time"

	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
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
// the first of them expires.
type layerGrant struct {
	issued, expires time.Time
}

// due reports whether a third of the grant's life has passed.
func (g layerGrant) due(now time.Time) bool {
	return !now.Before(g.issued.Add(g.expires.Sub(g.issued) / 3))
}

// layerCache holds the grants issued for each reference during one sync of
// one host, so the replicas of one image cost one query and one set of
// signatures.
type layerCache map[string][]*hostproto.LayerGrant

// layers returns grants for every layer of reference on host. Layers its
// region's copy of the layer bucket is not confirmed to hold read from the
// layer bucket, and a check of that copy starts.
func (s *Server) layers(ctx context.Context, host compute.HostID, cache layerCache, reference string) ([]*hostproto.LayerGrant, error) {
	if grants, ok := cache[reference]; ok {
		return grants, nil
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
	cache[reference] = out
	return out, nil
}

// maxReplicaChecks bounds the checks of layer copies one server runs at
// once. Each layer and region is checked once across the replicas, so this
// bounds only how fast new images reach new regions.
const maxReplicaChecks = 4

// replicaChecks runs the checks of regional layer copies grants ask for,
// one per reference and region, under the server's lifetime.
type replicaChecks struct {
	mu      sync.Mutex
	running map[string]bool
	wg      sync.WaitGroup
}

// confirmReplicas starts checking region's copy for the layers of
// reference unless this server already is, or runs maxReplicaChecks. The
// next grant of the reference asks again.
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
		if _, err := s.images.ConfirmReplicas(s.lifetime, reference, region); err != nil && s.lifetime.Err() == nil {
			s.logger.WarnContext(s.lifetime, "checking regional layer copies failed", "image", reference, "region", region, "error", err)
		}
	})
}

// grantOf is the life of layers issued at issued.
func (s *Server) grantOf(layers []*hostproto.LayerGrant, issued time.Time) layerGrant {
	g := layerGrant{issued: issued, expires: issued.Add(s.layerLifetime)}
	for _, l := range layers {
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
		sess.layers[image] = sess.server.grantOf(layers, now)
	}
	for image := range sess.layers {
		if !live[image] {
			delete(sess.layers, image)
		}
	}
	return nil
}
