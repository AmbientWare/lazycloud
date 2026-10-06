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

// maxNamedPlatformImages bounds the platform images a Hello names, and
// maxRunningPlatformImages the copies it says the host's containers run:
// a few releases' worth.
const (
	maxNamedPlatformImages   = 16
	maxRunningPlatformImages = 16
)

// unknownPlatformImage is the failure of a platform image the agent names
// that this server's release does not, as an agent of another release
// does until it updates.
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

// convertPlatform starts converting reference for architecture unless this
// server already is, or runs maxPlatformConversions. A session that still
// waits asks again at its next sync.
func (s *Server) convertPlatform(reference, architecture, traceparent string) {
	key := reference + " " + architecture
	s.platform.mu.Lock()
	defer s.platform.mu.Unlock()
	if s.platform.running[key] {
		return
	}
	select {
	case s.platform.slots <- struct{}{}:
	default:
		return
	}
	s.platform.running[key] = true
	s.platform.wg.Go(func() { s.runPlatform(key, reference, architecture, traceparent) })
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
				select {
				case s.platform.slots <- struct{}{}:
				case <-s.lifetime.Done():
					return
				}
				key := reference + " " + architecture
				s.platform.mu.Lock()
				running := s.platform.running[key]
				s.platform.running[key] = true
				s.platform.mu.Unlock()
				if running {
					<-s.platform.slots
					continue
				}
				s.runPlatform(key, reference, architecture, "")
			}
		}
	})
}

// runPlatform converts reference for architecture, then frees the slot and
// key convertPlatform or ConvertAtStart took. The conversion is a child of
// the start traceparent names that asked for it, or a trace of its own.
func (s *Server) runPlatform(key, reference, architecture, traceparent string) {
	defer func() {
		s.platform.mu.Lock()
		delete(s.platform.running, key)
		s.platform.mu.Unlock()
		<-s.platform.slots
	}()
	ctx, span := telemetry.StartIn(s.lifetime, s.tracer, traceparent, "images.convert_platform", trace.WithAttributes(
		attribute.String(telemetry.AttrImage, reference), attribute.String("lazycloud.architecture", architecture)))
	err := s.images.ConvertPlatformImage(ctx, reference, architecture)
	telemetry.Fail(span, err)
	if err == nil || s.lifetime.Err() != nil {
		return
	}
	var unconvertible *images.ConversionError
	switch {
	case errors.Is(err, images.ErrServerDisk):
		s.platform.failures.WithLabelValues("server_disk").Inc()
		s.logger.ErrorContext(s.lifetime, "the server cannot convert platform images on its disk; hosts get the failure until its temporary directory is writable",
			"image", reference, "architecture", architecture, "error", err)
	case errors.As(err, &unconvertible):
		s.platform.failures.WithLabelValues("image").Inc()
		s.logger.WarnContext(s.lifetime, "converting a platform image failed", "image", reference, "architecture", architecture, "error", err)
	default:
		s.platform.failures.WithLabelValues("transient").Inc()
		s.logger.WarnContext(s.lifetime, "converting a platform image failed", "image", reference, "architecture", architecture, "error", err)
	}
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
	ctx, span := telemetry.StartIn(ctx, sess.server.tracer, "", "hostsession.platform_images", trace.WithAttributes(telemetry.Host(sess.host.String())))
	defer span.End()
	var out []*hostproto.PlatformImage
	var known []string
	for _, reference := range p.named {
		switch {
		case platformimages.Known(reference):
			known = append(known, reference)
		case p.failed[reference].reason == "":
			p.failed[reference] = platformFailure{reason: unknownPlatformImage}
			out = append(out, &hostproto.PlatformImage{Reference: reference, Failure: unknownPlatformImage})
		}
	}
	pulls, err := sess.server.images.PlatformPulls(ctx, sess.host, known)
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
	copies, err := sess.server.images.PlatformCopies(ctx, p.running)
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
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
			sess.server.convertPlatform(pull.Reference, pull.Architecture, "")
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
	layers, err := sess.server.layers(ctx, sess.host, cache, pull.Pull.Reference)
	switch {
	case errors.Is(err, images.ErrNotConverted):
		return nil, true, nil
	case err != nil && ctx.Err() != nil:
		return nil, false, sess.server.grpcError(ctx, ctx.Err())
	case err != nil:
		sess.server.logger.WarnContext(ctx, "issuing platform image grants failed", "host", sess.host.String(), "image", pull.Pull.Reference, "error", err)
		return nil, false, nil
	}
	sess.platform.sent[key] = sess.server.grantOf(cache, pull.Pull.Reference, now)
	return &hostproto.PlatformImage{
		Reference: pull.Reference, Image: pull.Pull.Reference, Auth: registryAuthOut(pull.Pull.Auth), Platform: pull.Pull.Platform, Layers: layers,
	}, false, nil
}
