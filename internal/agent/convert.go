package agent

import (
	"context"
	"errors"
	"fmt"
	"os"
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

// convertedLayer is a layer converted to a data file under the build's
// directory and an index, kept until its pair is uploaded.
type convertedLayer struct {
	data      string
	dataBytes int64
	index     []byte
}

// layerPublish converts and uploads the layers the server names for one
// build, each in a goroutine of its own that wait waits for, so layers
// upload while others convert. Only the build's publishBuild uses it.
type layerPublish struct {
	a       *Agent
	build   *hostproto.ImageBuild
	dir     string
	logs    *buildLogs
	options []name.Option
	auth    authn.Authenticator
	// converting and uploading hold a token for each conversion and each
	// upload that runs.
	converting, uploading chan struct{}
	running               sync.WaitGroup
	// ready is signalled when a conversion or upload ends.
	ready chan struct{}

	mu     sync.Mutex
	layers map[string]*publishedLayer
	err    error
}

// publishedLayer is what the agent did with one layer and has yet to report.
type publishedLayer struct {
	converted *convertedLayer
	// rounds counts the times the server named the layer and the agent
	// converted, sized or uploaded it.
	rounds int
	// busy is set while its conversion or upload runs.
	busy     bool
	sized    *hostproto.ConvertedLayer
	uploaded *hostproto.UploadedLayer
}

func newLayerPublish(a *Agent, build *hostproto.ImageBuild, dir string, logs *buildLogs) *layerPublish {
	options, auth := registryAccess(build)
	return &layerPublish{
		a: a, build: build, dir: dir, logs: logs, options: options, auth: auth,
		converting: make(chan struct{}, maxConversions), uploading: make(chan struct{}, maxConversions),
		ready: make(chan struct{}, 1), layers: map[string]*publishedLayer{},
	}
}

// registryAccess is how the agent reads the build's push repository.
func registryAccess(build *hostproto.ImageBuild) ([]name.Option, authn.Authenticator) {
	var options []name.Option
	if build.GetInsecureRegistry() {
		options = append(options, name.Insecure)
	}
	auth := authn.Anonymous
	registry, _, _ := strings.Cut(build.GetPushRepository(), "/")
	if login := build.GetRegistryAuth()[registry]; login != nil {
		auth = authn.FromConfig(authn.AuthConfig{Username: login.GetUsername(), Password: login.GetPassword(), IdentityToken: login.GetIdentityToken()})
	}
	return options, auth
}

// publish sends request through complete, then acts on the layers each
// answer names and sends what that produced, until an answer names none.
// It returns once its conversions and uploads end.
func (p *layerPublish) publish(ctx context.Context, request *hostproto.CompleteImageBuildRequest,
	complete func(*hostproto.CompleteImageBuildRequest) *hostproto.CompleteImageBuildResponse,
) error {
	ctx, cancel := context.WithCancel(ctx)
	defer p.wait()
	defer cancel()
	began := time.Now()
	for {
		resp := complete(request)
		uploads := resp.GetLayerUploads()
		if len(uploads) == 0 {
			if n := p.count(); resp != nil && n > 0 {
				p.logs.add(fmt.Sprintf("converted and stored %d layers in %s", n, time.Since(began).Round(time.Millisecond)))
			}
			return nil
		}
		if err := p.start(ctx, uploads); err != nil {
			return err
		}
		converted, uploaded, err := p.next(ctx)
		if err != nil {
			return err
		}
		request.ConvertedLayers, request.UploadedLayers = converted, uploaded
	}
}

// start acts on each layer uploads names that is neither running nor
// waiting to be reported: it converts a layer not yet converted, uploads a
// converted one the server sent URLs for, and sizes the others again. A
// layer named more than maxPublishRounds times is an error.
func (p *layerPublish) start(ctx context.Context, uploads []*hostproto.LayerUpload) error {
	p.mu.Lock()
	defer p.mu.Unlock()
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
		switch {
		case l.converted == nil:
			l.busy = true
			conversions++
			p.running.Go(func() {
				converted, err := p.convert(ctx, u)
				p.finish(l, err, func() { l.converted, l.sized = converted, sizes(u, converted) })
			})
		case u.GetIndexUrl() != "":
			l.busy = true
			converted := l.converted
			p.running.Go(func() {
				uploaded, err := p.upload(ctx, converted, u)
				p.finish(l, err, func() { l.uploaded = uploaded })
			})
		default:
			l.sized = sizes(u, l.converted)
		}
	}
	if conversions > 0 {
		p.logs.add(fmt.Sprintf("converting %d layers", conversions))
	}
	return nil
}

func sizes(u *hostproto.LayerUpload, l *convertedLayer) *hostproto.ConvertedLayer {
	return &hostproto.ConvertedLayer{BlobDigest: u.GetBlobDigest(), DataBytes: l.dataBytes, IndexBytes: int64(len(l.index))}
}

// finish records the end of l's conversion or upload: done on success, the
// first error of the build otherwise.
func (p *layerPublish) finish(l *publishedLayer, err error, done func()) {
	p.mu.Lock()
	l.busy = false
	switch {
	case err == nil:
		done()
	case p.err == nil:
		p.err = err
	}
	p.mu.Unlock()
	select {
	case p.ready <- struct{}{}:
	default:
	}
}

// next waits until a layer has something to report, or none runs, and
// returns the sizes and uploads to report. A conversion or upload that
// failed is the error.
func (p *layerPublish) next(ctx context.Context) ([]*hostproto.ConvertedLayer, []*hostproto.UploadedLayer, error) {
	for {
		p.mu.Lock()
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
		err := p.err
		p.mu.Unlock()
		if err != nil || len(sized) > 0 || len(uploaded) > 0 || !busy {
			return sized, uploaded, err
		}
		select {
		case <-p.ready:
		case <-ctx.Done():
			return nil, nil, fmt.Errorf("convert layers: %w", ctx.Err())
		}
	}
}

// wait waits for every conversion and upload to end.
func (p *layerPublish) wait() { p.running.Wait() }

// count is how many layers the server named.
func (p *layerPublish) count() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.layers)
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
func (p *layerPublish) convert(ctx context.Context, u *hostproto.LayerUpload) (*convertedLayer, error) {
	if err := acquire(ctx, p.converting); err != nil {
		return nil, fmt.Errorf("convert layer %s: %w", u.GetBlobDigest(), err)
	}
	defer func() { <-p.converting }()
	ref, err := name.NewDigest(p.build.GetPushRepository()+"@"+u.GetBlobDigest(), p.options...)
	if err != nil {
		return nil, fmt.Errorf("layer %s: %w", u.GetBlobDigest(), err)
	}
	start := time.Now()
	var l *convertedLayer
	var ix imagefs.Index
	layerCtx, span := telemetry.Start(ctx, "agent.convert_layer", trace.WithAttributes(attribute.String(telemetry.AttrLayer, u.GetDiffId())))
	defer span.End()
	err = retryTransfer(layerCtx, func() error {
		var err error
		l, ix, err = p.convertLayer(layerCtx, ref, p.auth, u.GetDiffId())
		return err
	})
	span.SetAttributes(attribute.Int64("lazycloud.bytes", ix.DataSize), attribute.Int("lazycloud.files", len(ix.Entries)))
	telemetry.Fail(span, err)
	if err != nil {
		return nil, fmt.Errorf("convert layer %s: %w", u.GetBlobDigest(), err)
	}
	p.logs.add(fmt.Sprintf("converted %s: %d files, %d MB stored, in %s",
		u.GetBlobDigest(), len(ix.Entries), ix.DataSize>>20, time.Since(start).Round(time.Millisecond)))
	return l, nil
}

// convertLayer reads one layer from the registry and converts it into a
// data file under the build's directory. The file lives only until its
// upload, and a host that stops abandons the build, so it is not synced.
func (p *layerPublish) convertLayer(ctx context.Context, ref name.Digest, auth authn.Authenticator, diffID string) (*convertedLayer, imagefs.Index, error) {
	layer, err := remote.Layer(ref, remote.WithContext(ctx), remote.WithAuth(auth))
	if err != nil {
		return nil, imagefs.Index{}, fmt.Errorf("read layer: %w", err)
	}
	tarball, err := layer.Uncompressed()
	if err != nil {
		return nil, imagefs.Index{}, fmt.Errorf("read layer: %w", err)
	}
	defer func() { _ = tarball.Close() }()
	data, err := os.CreateTemp(p.dir, "layer-*.data")
	if err != nil {
		return nil, imagefs.Index{}, fmt.Errorf("create layer data file: %w", err)
	}
	ix, index, err := imagefs.ConvertLayer(ctx, tarball, data, imagefs.Digest(diffID))
	if closeErr := data.Close(); err == nil && closeErr != nil {
		err = fmt.Errorf("write layer data: %w", closeErr)
	}
	if err != nil {
		_ = os.Remove(data.Name())
	}
	if errors.Is(err, imagefs.ErrInvalidLayer) {
		return nil, imagefs.Index{}, fmt.Errorf("%w: %w", errLayerContent, err)
	}
	if err != nil {
		return nil, imagefs.Index{}, err //nolint:wrapcheck // The caller names the layer.
	}
	return &convertedLayer{data: data.Name(), dataBytes: ix.DataSize, index: index}, ix, nil
}

// upload stores l's data parts, then its index, and returns the parts'
// ETags.
func (p *layerPublish) upload(ctx context.Context, l *convertedLayer, u *hostproto.LayerUpload) (*hostproto.UploadedLayer, error) {
	if err := acquire(ctx, p.uploading); err != nil {
		return nil, fmt.Errorf("upload layer %s: %w", u.GetBlobDigest(), err)
	}
	defer func() { <-p.uploading }()
	start := time.Now()
	uploadCtx, span := telemetry.Start(ctx, "agent.upload_layer", trace.WithAttributes(
		attribute.String(telemetry.AttrLayer, u.GetDiffId()), attribute.Int64("lazycloud.bytes", l.dataBytes+int64(len(l.index)))))
	defer span.End()
	etags, err := p.a.uploadLayer(uploadCtx, l, u)
	telemetry.Fail(span, err)
	if err != nil {
		return nil, fmt.Errorf("upload layer %s: %w", u.GetBlobDigest(), err)
	}
	p.logs.add(fmt.Sprintf("uploaded %s in %s", u.GetBlobDigest(), time.Since(start).Round(time.Millisecond)))
	return &hostproto.UploadedLayer{BlobDigest: u.GetBlobDigest(), PartEtags: etags}, nil
}

// uploadLayer PUTs l's data in the parts upload names, then its index.
func (a *Agent) uploadLayer(ctx context.Context, l *convertedLayer, upload *hostproto.LayerUpload) ([]string, error) {
	data, err := os.Open(l.data)
	if err != nil {
		return nil, fmt.Errorf("open layer data: %w", err)
	}
	defer func() { _ = data.Close() }()
	return imagefs.UploadPair(ctx, a.http, data, l.dataBytes, l.index, //nolint:wrapcheck // The caller names the layer.
		upload.GetDataPartUrls(), upload.GetDataPartBytes(), upload.GetIndexUrl(), retryTransfer)
}
