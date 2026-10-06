package hostsession

import (
	"context"
	"errors"
	"slices"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/platformimages"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// maxPlatformConversions bounds the platform image conversions one server
// runs at once. Each runs once per reference and architecture across the
// replicas, so this bounds only the work hosts naming new images can ask
// for.
const maxPlatformConversions = 2

// maxHelloPlatformImages bounds both the platform images a Hello names and
// the copies it says the host's containers run: a few releases' worth. It
// bounds the work, not what a host may read.
const maxHelloPlatformImages = 16

// unknownPlatformImage is the failure of a platform image the agent names
// that this server's release does not, as an agent of another release
// does until it updates. The session stays open so the agent gets its
// update.
const unknownPlatformImage = "not a platform image of this release"

// platformConversions runs the platform image conversions sessions and the
// server's start ask for, one per reference and architecture, under the
// server's lifetime. slots holds one token per conversion running.
type platformConversions struct {
	mu      sync.Mutex
	running map[string]bool
	slots   chan struct{}
	wg      sync.WaitGroup
	// failures counts the conversions that failed, by cause.
	failures *prometheus.CounterVec
}

// claim takes a slot and the key of reference and architecture, waiting for
// a slot until ctx ends when wait is set. It reports false when it took
// neither: the slots are full, or this server converts the image already.
func (c *platformConversions) claim(ctx context.Context, key string, wait bool) bool {
	select {
	case c.slots <- struct{}{}:
	default:
		if !wait {
			return false
		}
		select {
		case c.slots <- struct{}{}:
		case <-ctx.Done():
			return false
		}
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.running[key] {
		<-c.slots
		return false
	}
	c.running[key] = true
	return true
}

// release frees the slot and key claim took.
func (c *platformConversions) release(key string) {
	c.mu.Lock()
	delete(c.running, key)
	c.mu.Unlock()
	<-c.slots
}

// convertPlatform starts converting reference for architecture unless this
// server already is, or runs maxPlatformConversions. A session that still
// waits asks again at its next sync.
func (s *Server) convertPlatform(reference, architecture, traceparent string) {
	key := reference + " " + architecture
	if s.platform.claim(s.lifetime, key, false) {
		s.platform.wg.Go(func() { s.runPlatform(key, reference, architecture, traceparent) })
	}
}

// ConvertAtStart converts platform, the platform images agents of this
// release name, and the managed Python image of every Python version, for
// each of architectures, in the background and within
// maxPlatformConversions, so the first starts after a deploy find them
// converted. Images converted or converting elsewhere cost one claim each;
// a failure is logged, and a start that needs the image, or a host of
// another architecture, converts it on demand. Wait waits for it.
func (s *Server) ConvertAtStart(platform, architectures []string) {
	s.platform.wg.Go(func() {
		references := slices.Clone(platform)
		for _, python := range images.PythonVersions() {
			source, err := s.images.ManagedSource(s.lifetime, python)
			if s.lifetime.Err() != nil {
				return
			}
			if err != nil {
				s.logger.WarnContext(s.lifetime, "pinning the managed image failed", "python", python, "error", err)
				continue
			}
			references = append(references, source)
		}
		for _, architecture := range architectures {
			for _, reference := range references {
				key := reference + " " + architecture
				if s.platform.claim(s.lifetime, key, true) {
					s.runPlatform(key, reference, architecture, "")
				}
				if s.lifetime.Err() != nil {
					return
				}
			}
		}
	})
}

// runPlatform converts reference for architecture, then releases its
// claim. The conversion is a child of the start traceparent names that
// asked for it, or a trace of its own.
func (s *Server) runPlatform(key, reference, architecture, traceparent string) {
	defer s.platform.release(key)
	ctx, span := telemetry.StartIn(s.lifetime, s.tracer, traceparent, "images.convert_platform", trace.WithAttributes(
		attribute.String(telemetry.AttrImage, reference), attribute.String("lazycloud.architecture", architecture)))
	err := s.images.ConvertPlatformImage(ctx, reference, architecture)
	telemetry.Fail(span, err)
	if err == nil || s.lifetime.Err() != nil {
		return
	}
	cause, log, msg := "transient", s.logger.WarnContext, "converting a platform image failed"
	var unconvertible *images.ConversionError
	switch {
	case errors.Is(err, images.ErrServerDisk):
		cause, log = "server_disk", s.logger.ErrorContext
		msg = "the server cannot convert platform images on its disk; hosts get the failure until its temporary directory is writable"
	case errors.As(err, &unconvertible):
		cause = "image"
	}
	s.platform.failures.WithLabelValues(cause).Inc()
	log(s.lifetime, msg, "image", reference, "architecture", architecture, "error", err)
}

// platformState is what a session answered for the platform images its
// Hello named and the copies it said its containers run. answers holds,
// per named reference or copy, when to look at it again: the renewal of
// the grants sent or the retry of the failure sent. One not answered, such
// as an image still converting or grants that could not be issued, is
// due. failed holds the failure last sent per named reference. running
// keeps only the copies that need grants of their own once the first
// answer showed which.
type platformState struct {
	named   []string
	running []string
	answers map[string]layerGrant
	failed  map[string]string
}

func (p *platformState) due(now time.Time) bool {
	for _, key := range slices.Concat(p.named, p.running) {
		if _, ok := p.answers[key]; !ok {
			return true
		}
	}
	return anyDue(p.answers, now)
}

// syncPlatform sends the platform images the host named once each is
// converted, with grants and a pull login, and again before the grants
// are due; and the reason of each that cannot be converted. It starts the
// conversion of each still converting and waits for its outcome. The
// copies the host's containers run get their grants renewed the same way,
// under the reference each copies.
func (sess *session) syncPlatform(ctx context.Context, cache *syncCache) error {
	p := &sess.platform
	now := time.Now()
	if !p.due(now) {
		return nil
	}
	ctx, span := telemetry.Start(ctx, "hostsession.platform_images", trace.WithAttributes(telemetry.Host(sess.host.String())))
	defer span.End()
	var out []*hostproto.PlatformImage
	fail := func(reference, reason string, retryAt time.Time) {
		if p.failed[reference] != reason {
			out = append(out, &hostproto.PlatformImage{Reference: reference, Failure: reason})
		}
		p.failed[reference] = reason
		if retryAt.IsZero() {
			// No conversion accepts it.
			retryAt = now.AddDate(100, 0, 0)
		}
		p.answers[reference] = layerGrant{renewAt: retryAt}
	}
	var known []string
	for _, reference := range p.named {
		if platformimages.Known(reference) {
			known = append(known, reference)
		} else {
			fail(reference, unknownPlatformImage, time.Time{})
		}
	}
	var pulls []images.PlatformPull
	if len(known) > 0 {
		var err error
		if pulls, err = sess.server.images.PlatformPulls(ctx, sess.host, known); err != nil {
			return sess.server.grpcError(ctx, err)
		}
	}
	copies, err := sess.server.images.PlatformCopies(ctx, p.running)
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
	// A copy that is a named image's current pull is answered under that
	// name, and one the server never recorded gets nothing.
	current := map[string]bool{}
	for _, pull := range pulls {
		if pull.Pull != nil {
			current[pull.Pull.Reference] = true
		}
	}
	named := len(pulls)
	p.running = p.running[:0]
	for _, c := range copies {
		if !current[c.Pull.Reference] {
			p.running = append(p.running, c.Pull.Reference)
			pulls = append(pulls, c)
		}
	}
	type due struct {
		key  string
		pull images.PlatformPull
	}
	var answer []due
	var mirrors []string
	for n, pull := range pulls {
		key := pull.Reference
		if n >= named {
			key = pull.Pull.Reference
		}
		if pull.Failure != "" {
			fail(key, pull.Failure, pull.RetryAt)
			continue
		}
		// A failure may be converted elsewhere before its retry.
		if g, ok := p.answers[key]; ok && !g.due(now) && p.failed[key] == "" {
			continue
		}
		answer = append(answer, due{key, pull})
		if pull.Pull != nil {
			mirrors = append(mirrors, pull.Pull.Reference)
		}
	}
	sess.signLayers(ctx, cache, mirrors)
	var used []string
	for _, a := range answer {
		key, pull := a.key, a.pull
		var layers []*hostproto.LayerGrant
		var grant layerGrant
		err := images.ErrNotConverted
		if pull.Pull != nil {
			layers, grant, err = sess.grantLayers(ctx, cache, pull.Pull.Reference)
		}
		if errors.Is(err, images.ErrNotConverted) {
			sess.server.convertPlatform(pull.Reference, pull.Architecture, "") //nolint:contextcheck // Conversions run under the server's lifetime.
			cache.waits[images.PlatformConverted] = true
			continue
		}
		if err != nil {
			if err := sess.grantFailed(ctx, pull.Pull.Reference, err); err != nil {
				return err
			}
			continue
		}
		p.answers[key] = grant
		delete(p.failed, key)
		used = append(used, pull.Pull.Reference)
		out = append(out, &hostproto.PlatformImage{
			Reference: pull.Reference, Image: pull.Pull.Reference, Auth: registryAuthOut(pull.Pull.Auth), Platform: pull.Pull.Platform, Layers: layers,
		})
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
	return sess.stream.Send(msg) //nolint:wrapcheck // The stream's status ends the session.
}
