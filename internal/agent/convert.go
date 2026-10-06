package agent

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"
	"sync"
	"time"

	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// maxConversions bounds the layers one build converts at once, since
	// each conversion takes a core, and separately those it uploads.
	maxConversions = 4
	// maxPublishRounds bounds the times the server may name one layer and
	// the agent act on it: once to convert and size it, once to upload it.
	maxPublishRounds = 4
	// publishCallTimeout bounds one CompleteImageBuild call, which reads the
	// pushed image and checks every reported pair.
	publishCallTimeout = time.Minute
	// transferAttempts bounds the tries of one layer read or object PUT
	// that fails for the network or the store, with backoff from
	// transferBackoff.
	transferAttempts = 5
	transferBackoff  = 500 * time.Millisecond
)

// errLayerContent marks a layer whose content conversion refuses: a tar it
// cannot index, or bytes that are not the layer the config names. Retrying
// does not help.
var errLayerContent = errors.New("the layer cannot be converted")

// retryTransfer runs fn until it succeeds, fails for the layer's content or
// the store's refusal, or uses its attempts.
func retryTransfer(ctx context.Context, fn func() error) error {
	content := func(err error) bool { return errors.Is(err, errLayerContent) }
	return imagefs.Retry(ctx, transferAttempts, transferBackoff, content, fn) //nolint:wrapcheck // fn's error.
}

// layerPublish converts and uploads the layers the server names for one
// build. Each conversion and upload runs on a goroutine of its own, so
// layers upload while others convert, and hands its result to publish,
// which alone reads and writes the layers' state.
type layerPublish struct {
	c       *container
	build   *hostproto.ImageBuild
	dir     string
	logs    buildLogs
	options []name.Option
	auth    authn.Authenticator
	// converting and uploading hold a token for each conversion and each
	// upload that runs.
	converting, uploading chan struct{}
	running               sync.WaitGroup
	results               chan layerResult
	layers                map[string]*publishedLayer
}

// publishedLayer is what the agent did with one layer and has yet to report.
type publishedLayer struct {
	// converted is the layer's data file under the build's directory and
	// its index, kept until the pair is uploaded.
	converted *imagefs.ConvertedFile
	// rounds counts the times the server named the layer and the agent
	// converted, sized or uploaded it.
	rounds int
	// busy is set while its conversion or upload runs.
	busy     bool
	sized    *hostproto.ConvertedLayer
	uploaded *hostproto.UploadedLayer
}

// layerResult is the end of a layer's conversion or upload; done records
// what it produced.
type layerResult struct {
	layer *publishedLayer
	done  func()
	err   error
}

func newLayerPublish(c *container, build *hostproto.ImageBuild, dir string, logs buildLogs) *layerPublish {
	p := &layerPublish{
		c: c, build: build, dir: dir, logs: logs, auth: authn.Anonymous,
		converting: make(chan struct{}, maxConversions), uploading: make(chan struct{}, maxConversions),
		results: make(chan layerResult), layers: map[string]*publishedLayer{},
	}
	if build.GetInsecureRegistry() {
		p.options = append(p.options, name.Insecure)
	}
	registry, _, _ := strings.Cut(build.GetPushRepository(), "/")
	if login := build.GetRegistryAuth()[registry]; login != nil {
		p.auth = authn.FromConfig(authn.AuthConfig{Username: login.GetUsername(), Password: login.GetPassword(), IdentityToken: login.GetIdentityToken()})
	}
	return p
}

// publish completes the build with request, then acts on the layers each
// answer names and sends what that produced, until an answer names none.
// It returns once its conversions and uploads end.
func (p *layerPublish) publish(ctx context.Context, request *hostproto.CompleteImageBuildRequest) error {
	ctx, cancel := context.WithCancel(ctx)
	defer p.running.Wait()
	defer cancel()
	began := time.Now()
	for {
		resp := p.c.completeBuild(ctx, request)
		uploads := resp.GetLayerUploads()
		if len(uploads) == 0 {
			if resp != nil && len(p.layers) > 0 {
				p.logs.add(ctx, fmt.Sprintf("converted and stored %d layers in %s", len(p.layers), time.Since(began).Round(time.Millisecond)))
			}
			return nil
		}
		if err := p.start(ctx, uploads); err != nil {
			return err
		}
		var err error
		if request.ConvertedLayers, request.UploadedLayers, err = p.next(ctx); err != nil {
			return err
		}
	}
}

// start acts on each layer uploads names that is neither running nor
// waiting to be reported: it converts a layer not yet converted, uploads a
// converted one the server sent URLs for, and sizes the others again. A
// layer named more than maxPublishRounds times is an error.
func (p *layerPublish) start(ctx context.Context, uploads []*hostproto.LayerUpload) error {
	conversions := 0
	for _, u := range uploads {
		l := p.layers[u.GetBlobDigest()]
		if l == nil {
			l = &publishedLayer{}
			p.layers[u.GetBlobDigest()] = l
		}
		if l.busy || l.sized != nil || l.uploaded != nil {
			continue
		}
		if l.rounds++; l.rounds > maxPublishRounds {
			return fmt.Errorf("layer %s was still not stored after %d rounds", u.GetBlobDigest(), maxPublishRounds)
		}
		switch converted := l.converted; {
		case converted == nil:
			conversions++
			p.run(ctx, l, func() (func(), error) {
				converted, err := p.convert(ctx, u)
				return func() { l.converted, l.sized = converted, sizes(u, converted) }, err
			})
		case u.GetIndexUrl() != "":
			p.run(ctx, l, func() (func(), error) {
				uploaded, err := p.upload(ctx, converted, u)
				return func() { l.uploaded = uploaded }, err
			})
		default:
			l.sized = sizes(u, converted)
		}
	}
	if conversions > 0 {
		p.logs.add(ctx, fmt.Sprintf("converting %d layers", conversions))
	}
	return nil
}

func sizes(u *hostproto.LayerUpload, l *imagefs.ConvertedFile) *hostproto.ConvertedLayer {
	return &hostproto.ConvertedLayer{BlobDigest: u.GetBlobDigest(), DataBytes: l.DataBytes, IndexBytes: int64(len(l.Index))}
}

// run runs l's conversion or upload on a goroutine publish waits for and
// hands its result to next.
func (p *layerPublish) run(ctx context.Context, l *publishedLayer, work func() (func(), error)) {
	l.busy = true
	p.running.Go(func() {
		done, err := work()
		select {
		case p.results <- layerResult{layer: l, done: done, err: err}:
		case <-ctx.Done():
		}
	})
}

// next waits until a layer has something to report, or none runs, and
// returns the sizes and uploads to report. A conversion or upload that
// failed is the error.
func (p *layerPublish) next(ctx context.Context) ([]*hostproto.ConvertedLayer, []*hostproto.UploadedLayer, error) {
	for {
		var sized []*hostproto.ConvertedLayer
		var uploaded []*hostproto.UploadedLayer
		busy := false
		for _, l := range p.layers {
			if l.sized != nil {
				sized = append(sized, l.sized)
			}
			if l.uploaded != nil {
				uploaded = append(uploaded, l.uploaded)
			}
			l.sized, l.uploaded = nil, nil
			busy = busy || l.busy
		}
		if len(sized) > 0 || len(uploaded) > 0 || !busy {
			return sized, uploaded, nil
		}
		select {
		case r := <-p.results:
			if err := r.record(); err != nil {
				return nil, nil, err
			}
		case <-ctx.Done():
			return nil, nil, fmt.Errorf("convert layers: %w", ctx.Err())
		}
		// Results that ended meanwhile report with it.
		for drained := false; !drained; {
			select {
			case r := <-p.results:
				if err := r.record(); err != nil {
					return nil, nil, err
				}
			default:
				drained = true
			}
		}
	}
}

// record applies r to its layer and returns its error.
func (r layerResult) record() error {
	r.layer.busy = false
	if r.err == nil {
		r.done()
	}
	return r.err
}

// acquire takes one of tokens, or fails when ctx ends first.
func acquire(ctx context.Context, tokens chan struct{}) error {
	select {
	case tokens <- struct{}{}:
		return nil
	case <-ctx.Done():
		return ctx.Err() //nolint:wrapcheck // The caller names the step.
	}
}

// convert converts one layer, read from the image the build pushed.
func (p *layerPublish) convert(ctx context.Context, u *hostproto.LayerUpload) (*imagefs.ConvertedFile, error) {
	if err := acquire(ctx, p.converting); err != nil {
		return nil, fmt.Errorf("convert layer %s: %w", u.GetBlobDigest(), err)
	}
	defer func() { <-p.converting }()
	ref, err := name.NewDigest(p.build.GetPushRepository()+"@"+u.GetBlobDigest(), p.options...)
	if err != nil {
		return nil, fmt.Errorf("layer %s: %w", u.GetBlobDigest(), err)
	}
	start := time.Now()
	layerCtx, span := telemetry.Start(ctx, "agent.convert_layer", trace.WithAttributes(attribute.String(telemetry.AttrLayer, u.GetDiffId())))
	var l *imagefs.ConvertedFile
	err = retryTransfer(layerCtx, func() error {
		var err error
		l, err = p.convertLayer(layerCtx, ref, u.GetDiffId())
		return err
	})
	if err == nil {
		span.SetAttributes(attribute.Int64("lazycloud.bytes", l.DataBytes), attribute.Int("lazycloud.files", l.Entries))
	}
	telemetry.Fail(span, err)
	if err != nil {
		return nil, fmt.Errorf("convert layer %s: %w", u.GetBlobDigest(), err)
	}
	p.logs.add(ctx, fmt.Sprintf("converted %s: %d files, %d MB stored, in %s",
		u.GetBlobDigest(), l.Entries, l.DataBytes>>20, time.Since(start).Round(time.Millisecond)))
	return l, nil
}

// convertLayer reads one layer from the registry and converts it into a
// data file under the build's directory. The file lives only until its
// upload, and a host that stops abandons the build, so it is not synced.
func (p *layerPublish) convertLayer(ctx context.Context, ref name.Digest, diffID string) (*imagefs.ConvertedFile, error) {
	layer, err := remote.Layer(ref, remote.WithContext(ctx), remote.WithAuth(p.auth))
	if err != nil {
		return nil, fmt.Errorf("read layer: %w", err)
	}
	tarball, err := layer.Uncompressed()
	if err != nil {
		return nil, fmt.Errorf("read layer: %w", err)
	}
	defer func() { _ = tarball.Close() }()
	source := &sourceRead{r: tarball}
	l, err := imagefs.ConvertFile(ctx, source, p.dir, imagefs.Digest(diffID))
	// A stream the registry cut off reads as a truncated tar, but reading
	// it again can succeed.
	if err != nil && source.err != nil {
		return nil, fmt.Errorf("read layer: %w", source.err)
	}
	if errors.Is(err, imagefs.ErrInvalidLayer) {
		return nil, fmt.Errorf("%w: %w", errLayerContent, err)
	}
	return l, err //nolint:wrapcheck // The caller names the layer.
}

// sourceRead reads r and keeps the first error other than io.EOF it gave.
type sourceRead struct {
	r   io.Reader
	err error
}

func (s *sourceRead) Read(p []byte) (int, error) {
	n, err := s.r.Read(p)
	if err != nil && !errors.Is(err, io.EOF) && s.err == nil {
		s.err = err
	}
	return n, err //nolint:wrapcheck // A reader passes its source's errors.
}

// upload stores l's data parts, then its index, and returns the parts'
// ETags.
func (p *layerPublish) upload(ctx context.Context, l *imagefs.ConvertedFile, u *hostproto.LayerUpload) (*hostproto.UploadedLayer, error) {
	if err := acquire(ctx, p.uploading); err != nil {
		return nil, fmt.Errorf("upload layer %s: %w", u.GetBlobDigest(), err)
	}
	defer func() { <-p.uploading }()
	start := time.Now()
	uploadCtx, span := telemetry.Start(ctx, "agent.upload_layer", trace.WithAttributes(
		attribute.String(telemetry.AttrLayer, u.GetDiffId()), attribute.Int64("lazycloud.bytes", l.DataBytes+int64(len(l.Index)))))
	etags, err := l.Upload(uploadCtx, p.c.a.http, u.GetDataPartUrls(), u.GetDataPartBytes(), u.GetIndexUrl(), retryTransfer)
	telemetry.Fail(span, err)
	if err != nil {
		return nil, fmt.Errorf("upload layer %s: %w", u.GetBlobDigest(), err)
	}
	p.logs.add(ctx, fmt.Sprintf("uploaded %s in %s", u.GetBlobDigest(), time.Since(start).Round(time.Millisecond)))
	return &hostproto.UploadedLayer{BlobDigest: u.GetBlobDigest(), PartEtags: etags}, nil
}
