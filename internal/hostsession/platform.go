package hostsession

import (
	"context"
	"errors"
	"sync"
	"time"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/images"
)

// maxPlatformConversions bounds the platform image conversions one server
// runs at once. Each runs once per reference and architecture across the
// replicas, so this bounds only the work hosts naming new images can ask
// for.
const maxPlatformConversions = 2

// maxRunningPlatformImages bounds the copies of platform images a Hello
// says the host's containers run: a few releases' worth.
const maxRunningPlatformImages = 16

// platformConversions runs the platform image conversions sessions ask
// for, one per reference and architecture, under the server's lifetime.
type platformConversions struct {
	mu      sync.Mutex
	running map[string]bool
	wg      sync.WaitGroup
}

// convertPlatform starts converting reference for architecture unless this
// server already is, or runs maxPlatformConversions. A session that still
// waits asks again at its next sync.
func (s *Server) convertPlatform(reference, architecture string) {
	key := reference + " " + architecture
	s.platform.mu.Lock()
	defer s.platform.mu.Unlock()
	if s.platform.running[key] || len(s.platform.running) >= maxPlatformConversions {
		return
	}
	s.platform.running[key] = true
	s.platform.wg.Go(func() {
		defer func() {
			s.platform.mu.Lock()
			delete(s.platform.running, key)
			s.platform.mu.Unlock()
		}()
		err := s.images.ConvertPlatformImage(s.lifetime, reference, architecture)
		if err != nil && s.lifetime.Err() == nil {
			s.logger.WarnContext(s.lifetime, "converting a platform image failed", "image", reference, "architecture", architecture, "error", err)
		}
	})
}

// platformState is what a session sent for the platform images its Hello
// named and the copies it said its containers run: the life of the grants
// sent per named reference or copy, and the failure sent for each named
// image that cannot be converted.
type platformState struct {
	named   []string
	running []string
	sent    map[string]layerGrant
	failed  map[string]platformFailure
	waiting bool
}

// platformFailure is a failure sent for a platform image and when it may
// be converted again; zero for never.
type platformFailure struct {
	reason  string
	retryAt time.Time
}

// due reports whether a named image was not answered yet, its failure's
// retry period passed, or any grants sent are due.
func (p *platformState) due(now time.Time) bool {
	for _, reference := range p.named {
		failed, isFailed := p.failed[reference]
		_, isSent := p.sent[reference]
		if !isSent && (!isFailed || (!failed.retryAt.IsZero() && !now.Before(failed.retryAt))) {
			return true
		}
	}
	for _, g := range p.sent {
		if g.due(now) {
			return true
		}
	}
	return false
}

// syncPlatform sends the platform images the host named once each is
// converted, with grants and a pull login, and again before the grants
// are due; and the reason of each that cannot be converted. It starts the
// conversion of each still waiting and records in waits the wake-up of its
// outcome. The copies the host's containers run get their grants renewed
// the same way. It reads the images only while one waits or is due.
func (sess *session) syncPlatform(ctx context.Context, cache layerCache, waits map[string]bool) error {
	p := &sess.platform
	now := time.Now()
	if len(p.named)+len(p.running) == 0 || (!p.waiting && !p.due(now)) {
		return nil
	}
	pulls, err := sess.server.images.PlatformPulls(ctx, sess.host, p.named)
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
	copies, err := sess.server.images.PlatformCopies(ctx, p.running)
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
	var out []*hostproto.PlatformImage
	var used []string
	p.waiting = false
	current := map[string]bool{}
	for _, pull := range pulls {
		if pull.Failure != "" {
			delete(p.sent, pull.Reference)
			if p.failed[pull.Reference].reason != pull.Failure {
				out = append(out, &hostproto.PlatformImage{Reference: pull.Reference, Failure: pull.Failure})
			}
			p.failed[pull.Reference] = platformFailure{reason: pull.Failure, retryAt: pull.RetryAt}
			continue
		}
		if pull.Pull != nil {
			current[pull.Pull.Reference] = true
		}
		image, waiting, err := sess.platformImage(ctx, cache, pull.Reference, pull, now)
		if err != nil {
			return err
		}
		if waiting {
			p.waiting = true
			sess.server.convertPlatform(pull.Reference, pull.Architecture)
			continue
		}
		if image != nil {
			delete(p.failed, pull.Reference)
			used = append(used, image.GetImage())
			out = append(out, image)
		}
	}
	for _, c := range copies {
		if current[c.Pull.Reference] {
			continue
		}
		image, _, err := sess.platformImage(ctx, cache, c.Pull.Reference, c, now)
		if err != nil {
			return err
		}
		if image != nil {
			used = append(used, image.GetImage())
			out = append(out, image)
		}
	}
	if p.waiting {
		waits[images.PlatformConverted] = true
	}
	// Grants keep the copies live through the layer sweep while hosts run.
	if err := sess.server.images.RecordUses(ctx, used); err != nil {
		return sess.server.grpcError(ctx, err)
	}
	if len(out) == 0 {
		return nil
	}
	msg := &hostproto.ServerMessage{
		CommandId: "platform-images:" + now.Format(time.RFC3339Nano),
		Body:      &hostproto.ServerMessage_PlatformImages{PlatformImages: &hostproto.PlatformImages{Images: out}},
	}
	if err := sess.stream.Send(msg); err != nil {
		return err //nolint:wrapcheck // The stream's status ends the session.
	}
	return nil
}

// platformImage is the answer for pull, sent under key: nil while its
// grants are not due, waiting while it is not converted. Grants that
// cannot be issued are logged and tried again at the next sync; the host
// keeps earlier ones until they expire.
func (sess *session) platformImage(ctx context.Context, cache layerCache, key string, pull images.PlatformPull, now time.Time) (*hostproto.PlatformImage, bool, error) {
	if g, ok := sess.platform.sent[key]; ok && !g.due(now) {
		return nil, false, nil
	}
	if pull.Pull == nil {
		return nil, true, nil
	}
	layers, err := sess.server.layers(ctx, cache, pull.Pull.Reference)
	switch {
	case errors.Is(err, images.ErrNotConverted):
		return nil, true, nil
	case err != nil && ctx.Err() != nil:
		return nil, false, sess.server.grpcError(ctx, ctx.Err())
	case err != nil:
		sess.server.logger.WarnContext(ctx, "issuing platform image grants failed", "host", sess.host.String(), "image", pull.Pull.Reference, "error", err)
		return nil, false, nil
	}
	sess.platform.sent[key] = sess.server.grantOf(layers, now)
	return &hostproto.PlatformImage{
		Reference: pull.Reference, Image: pull.Pull.Reference, Auth: registryAuthOut(pull.Pull.Auth), Platform: pull.Pull.Platform, Layers: layers,
	}, false, nil
}
