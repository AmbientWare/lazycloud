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
	"golang.org/x/sync/errgroup"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs"
)

const (
	// maxConversions bounds the layers one build converts or uploads at
	// once: each conversion takes a core.
	maxConversions = 4
	// maxPublishRounds bounds the completions one build makes while the
	// server still names layers: one to size them, one to upload them.
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
// build. Only the build's publishBuild uses it; its conversions run under
// mu.
type layerPublish struct {
	a     *Agent
	build *hostproto.ImageBuild
	dir   string
	logs  *buildLogs

	mu   sync.Mutex
	done map[string]*convertedLayer
}

// answer does what uploads ask: converts the layers without URLs and those
// not yet converted, which it reports sized, and uploads the others. A
// layer that cannot be converted or stored is an error.
func (p *layerPublish) answer(ctx context.Context, uploads []*hostproto.LayerUpload) ([]*hostproto.ConvertedLayer, []*hostproto.UploadedLayer, error) {
	var convert, upload []*hostproto.LayerUpload
	for _, u := range uploads {
		p.mu.Lock()
		_, converted := p.done[u.GetBlobDigest()]
		p.mu.Unlock()
		switch {
		case !converted:
			convert = append(convert, u)
		case u.GetIndexUrl() != "":
			upload = append(upload, u)
		default:
			convert = append(convert, u)
		}
	}
	if err := p.convert(ctx, convert); err != nil {
		return nil, nil, err
	}
	sized := make([]*hostproto.ConvertedLayer, len(convert))
	for n, u := range convert {
		p.mu.Lock()
		l := p.done[u.GetBlobDigest()]
		p.mu.Unlock()
		sized[n] = &hostproto.ConvertedLayer{BlobDigest: u.GetBlobDigest(), DataBytes: l.dataBytes, IndexBytes: int64(len(l.index))}
	}
	uploaded, err := p.upload(ctx, upload)
	return sized, uploaded, err
}

// convert converts each layer not yet converted, read from the image the
// build pushed.
func (p *layerPublish) convert(ctx context.Context, uploads []*hostproto.LayerUpload) error {
	var todo []*hostproto.LayerUpload
	p.mu.Lock()
	for _, u := range uploads {
		if p.done[u.GetBlobDigest()] == nil {
			todo = append(todo, u)
		}
	}
	p.mu.Unlock()
	if len(todo) == 0 {
		return nil
	}
	repository := p.build.GetPushRepository()
	options := []name.Option{}
	if p.build.GetInsecureRegistry() {
		options = append(options, name.Insecure)
	}
	auth := authn.Anonymous
	registry, _, _ := strings.Cut(repository, "/")
	if login := p.build.GetRegistryAuth()[registry]; login != nil {
		auth = authn.FromConfig(authn.AuthConfig{Username: login.GetUsername(), Password: login.GetPassword(), IdentityToken: login.GetIdentityToken()})
	}
	p.logs.add(fmt.Sprintf("converting %d layers", len(todo)))
	began := time.Now()
	g, ctx := errgroup.WithContext(ctx)
	g.SetLimit(maxConversions)
	for _, u := range todo {
		g.Go(func() error {
			ref, err := name.NewDigest(repository+"@"+u.GetBlobDigest(), options...)
			if err != nil {
				return fmt.Errorf("layer %s: %w", u.GetBlobDigest(), err)
			}
			start := time.Now()
			var l *convertedLayer
			var ix imagefs.Index
			err = retryTransfer(ctx, func() error {
				var err error
				l, ix, err = p.convertLayer(ctx, ref, auth, u.GetDiffId())
				return err
			})
			if err != nil {
				return fmt.Errorf("convert layer %s: %w", u.GetBlobDigest(), err)
			}
			p.mu.Lock()
			p.done[u.GetBlobDigest()] = l
			p.mu.Unlock()
			p.logs.add(fmt.Sprintf("converted %s: %d files, %d MB stored, in %s",
				u.GetBlobDigest(), len(ix.Entries), ix.DataSize>>20, time.Since(start).Round(time.Millisecond)))
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return err //nolint:wrapcheck // Each conversion's error names its layer.
	}
	p.logs.add(fmt.Sprintf("converted %d layers in %s", len(todo), time.Since(began).Round(time.Millisecond)))
	return nil
}

// convertLayer reads one layer from the registry and converts it into a
// data file under the build's directory.
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
	keep := false
	defer func() {
		_ = data.Close()
		if !keep {
			_ = os.Remove(data.Name())
		}
	}()
	ix, index, err := imagefs.ConvertLayer(ctx, tarball, data, imagefs.Digest(diffID))
	if errors.Is(err, imagefs.ErrInvalidLayer) {
		return nil, imagefs.Index{}, fmt.Errorf("%w: %w", errLayerContent, err)
	}
	if err != nil {
		return nil, imagefs.Index{}, err //nolint:wrapcheck // The caller names the layer.
	}
	if err := data.Sync(); err != nil {
		return nil, imagefs.Index{}, fmt.Errorf("write layer data: %w", err)
	}
	keep = true
	return &convertedLayer{data: data.Name(), dataBytes: ix.DataSize, index: index}, ix, nil
}

// upload stores each converted layer's data parts, then its index, and
// returns the parts' ETags.
func (p *layerPublish) upload(ctx context.Context, uploads []*hostproto.LayerUpload) ([]*hostproto.UploadedLayer, error) {
	out := make([]*hostproto.UploadedLayer, len(uploads))
	g, ctx := errgroup.WithContext(ctx)
	g.SetLimit(maxConversions)
	for n, u := range uploads {
		p.mu.Lock()
		l := p.done[u.GetBlobDigest()]
		p.mu.Unlock()
		g.Go(func() error {
			etags, err := p.a.uploadLayer(ctx, l, u)
			if err != nil {
				return fmt.Errorf("upload layer %s: %w", u.GetBlobDigest(), err)
			}
			out[n] = &hostproto.UploadedLayer{BlobDigest: u.GetBlobDigest(), PartEtags: etags}
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err //nolint:wrapcheck // Each upload's error names its layer.
	}
	return out, nil
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
