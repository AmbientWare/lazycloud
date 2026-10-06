package images

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/google/go-containerregistry/pkg/name"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/go-containerregistry/pkg/v1/tarball"
	"github.com/google/go-containerregistry/pkg/v1/types"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"golang.org/x/sync/errgroup"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// Platform images are the images agents run on their own and the managed
// images. No build can convert them, so the server converts each itself,
// once per reference and host architecture across its replicas, through
// the same offers and uploads a build's host is given.

const (
	// PlatformConverted is the ChannelImageBuild key a platform image
	// conversion announces its outcome on.
	PlatformConverted = "platform-images"
	// platformConversionTimeout bounds one conversion.
	platformConversionTimeout = 15 * time.Minute
	// platformLease is how long a conversion's lease lasts unless its owner
	// renews it, every third of it while it converts: an owner that died
	// gives the conversion up within about a minute.
	platformLease = time.Minute
	// A conversion that failed for something other than the image is tried
	// again after platformTransientRetry; one that failed for the image,
	// which only another image fixes, after platformFailureRetry.
	platformTransientRetry = 30 * time.Second
	platformFailureRetry   = 10 * time.Minute
	// maxPlatformImageBytes bounds the compressed layers of a platform
	// image, which the server converts on its own disk.
	maxPlatformImageBytes = 4 << 30
	// platformLayerParallelism bounds the layers one conversion converts or
	// uploads at once.
	platformLayerParallelism = 2
	// platformPullWindow is how long a platform image's pull login stays
	// valid at least once handed out: longer than a session takes to send
	// fresh grants, which come with a fresh login.
	platformPullWindow = 30 * time.Minute
	// platformTransferAttempts bounds the tries of one registry read or
	// store write that fails for the network or the service.
	platformTransferAttempts = 3
	platformTransferBackoff  = time.Second
)

// ErrServerDisk means the server could not create the files a platform
// image conversion keeps on its own disk: its temporary directory is
// missing, read-only or full. Only the server's deployment fixes it, so it
// is recorded as a failure hosts see and retried after
// platformFailureRetry, like a failure of the image.
var ErrServerDisk = errors.New("the server cannot write platform image conversion files")

// PlatformPull is how a host runs one platform image it named: Pull once
// the image is converted, or Failure when it cannot be. With neither the
// image is converting, or waits for ConvertPlatformImage to convert it.
type PlatformPull struct {
	Reference    string
	Architecture string
	Pull         *Pull
	Failure      string
	// RetryAt is when a recorded failure may be converted again; zero for
	// a reference no conversion accepts.
	RetryAt time.Time
	// Traceparent is the trace of the conversion last claimed.
	Traceparent string
}

// PlatformPulls returns how host pulls each of references, the platform
// images its agent named, for the host's architecture. A reference that
// is not a public image by digest is a Failure.
func (i *Images) PlatformPulls(ctx context.Context, host compute.HostID, references []string) ([]PlatformPull, error) {
	if len(references) == 0 {
		return nil, nil
	}
	rows, err := i.queries.PlatformImages(ctx, PlatformImagesParams{Host: uuid.UUID(host), Refs: references})
	if err != nil {
		return nil, fmt.Errorf("read platform images: %w", err)
	}
	architecture := deref(rows[0].HostArchitecture)
	if architecture == "" {
		return nil, fmt.Errorf("read platform images: host %s does not exist", uuid.UUID(host))
	}
	byReference := make(map[string]PlatformImagesRow, len(rows))
	for _, row := range rows {
		if row.Reference != nil {
			byReference[*row.Reference] = row
		}
	}
	out := make([]PlatformPull, len(references))
	for n, reference := range references {
		out[n] = PlatformPull{Reference: reference, Architecture: architecture}
		if _, err := i.platformSource(reference); err != nil {
			out[n].Failure = err.Error()
			continue
		}
		row := byReference[reference]
		out[n].Traceparent = deref(row.Traceparent)
		switch {
		case row.Converted:
			pull, err := i.pull(ctx, *row.Mirror, architecture, platformPullWindow)
			if err != nil {
				return nil, err
			}
			out[n].Pull = &pull
		case row.Failure != nil && row.FailedAt != nil && !*row.FailureTransient && time.Since(*row.FailedAt) < platformFailureRetry:
			out[n].Failure, out[n].RetryAt = *row.Failure, row.FailedAt.Add(platformFailureRetry)
		}
	}
	return out, nil
}

// PlatformCopies returns the converted platform image copies among
// mirrors, images the host's containers run, with the reference each
// copies. A copy an agent no longer names still needs its grants while
// containers run it. Others are left out.
func (i *Images) PlatformCopies(ctx context.Context, mirrors []string) ([]PlatformPull, error) {
	if len(mirrors) == 0 {
		return nil, nil
	}
	rows, err := i.queries.PlatformImages(ctx, PlatformImagesParams{Mirrors: mirrors})
	if err != nil {
		return nil, fmt.Errorf("read platform image copies: %w", err)
	}
	var out []PlatformPull
	for _, row := range rows {
		if row.Converted {
			out = append(out, PlatformPull{
				Reference: *row.Reference, Architecture: *row.Architecture,
				Pull: &Pull{Reference: *row.Mirror, Platform: "linux/" + *row.Architecture},
			})
		}
	}
	return out, nil
}

// platformSource parses reference, a platform image an agent named. It
// must name its digest and come from a public registry, or from the
// platform registry outside the workload repositories, which hold
// workspaces' images.
func (i *Images) platformSource(reference string) (name.Digest, error) {
	var options []name.Option
	if strings.HasPrefix(reference, i.config.Registry+"/") && i.config.Insecure {
		options = append(options, name.Insecure)
	}
	parsed, err := name.NewDigest(reference, options...)
	if err != nil {
		return name.Digest{}, invalid("platform image %q is not an image by digest", reference)
	}
	host := parsed.RegistryStr()
	if host == i.config.Registry {
		if _, workload := i.config.repositoryOf(reference); workload {
			return name.Digest{}, invalid("platform image %s is a workload image", reference)
		}
		return parsed, nil
	}
	if err := checkRegistryHost(host); err != nil {
		return name.Digest{}, err
	}
	return parsed, nil
}

// platformConversion is one leased conversion of a platform image for an
// architecture. Its outcome is recorded only under its token, and every
// record before that is idempotent, so an owner whose lease lapsed changes
// nothing another depends on.
type platformConversion struct {
	reference, architecture string
	token                   uuid.UUID
}

// ConvertPlatformImage converts reference, a platform image, for
// architecture unless it is converted, another replica's conversion holds
// its lease, or its last attempt failed within the retry period. The
// outcome is recorded and announced on ChannelImageBuild under
// PlatformConverted. A failure of the image is a ConversionError.
func (i *Images) ConvertPlatformImage(ctx context.Context, reference, architecture string) error {
	return i.convertPlatformImage(ctx, reference, architecture, platformLease)
}

func (i *Images) convertPlatformImage(ctx context.Context, reference, architecture string, lease time.Duration) error {
	source, err := i.platformSource(reference)
	if err != nil {
		return err
	}
	// The lease keeps one conversion of a reference and architecture at a
	// time.
	c := platformConversion{reference: reference, architecture: architecture, token: uuid.New()}
	claimed, err := i.queries.ClaimPlatformImage(ctx, ClaimPlatformImageParams{
		Reference: reference, Architecture: architecture, Token: c.token, LeaseSeconds: lease.Seconds(),
		TransientRetrySeconds: platformTransientRetry.Seconds(), FailureRetrySeconds: platformFailureRetry.Seconds(),
		Traceparent: traceparent(ctx),
	})
	trace.SpanFromContext(ctx).SetAttributes(attribute.Bool("lazycloud.claimed", err == nil))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("claim platform image: %w", err)
	}
	if claimed.Converted {
		return pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error { return c.finish(ctx, tx, *claimed.Mirror) }) //nolint:wrapcheck // finish names its step.
	}
	convertCtx, cancel := context.WithTimeout(ctx, platformConversionTimeout)
	defer cancel()
	renewCtx, lost := context.WithCancelCause(convertCtx)
	var renewals sync.WaitGroup
	renewals.Go(func() { i.renewPlatform(renewCtx, lost, c, lease) })
	err = i.convertPlatform(renewCtx, source, c)
	lost(nil)
	renewals.Wait()
	if err == nil {
		return nil
	}
	var unconvertible *ConversionError
	var rejected *InvalidError
	transient := !errors.As(err, &unconvertible) && !errors.As(err, &rejected) && !errors.Is(err, ErrServerDisk)
	record := context.WithoutCancel(ctx)
	failed := pgx.BeginFunc(record, i.pool, func(tx pgx.Tx) error {
		if err := i.queries.WithTx(tx).FailPlatformImage(record, FailPlatformImageParams{
			Reference: reference, Architecture: architecture, Token: c.token, Failure: truncate(err.Error()), Transient: transient,
		}); err != nil {
			return fmt.Errorf("record platform image failure: %w", err)
		}
		return database.Notify(record, tx, database.ChannelImageBuild, PlatformConverted)
	})
	return errors.Join(fmt.Errorf("convert platform image %s: %w", reference, err), failed)
}

// errLeaseLost ends a conversion whose lease another owner took.
var errLeaseLost = errors.New("another owner took the conversion's lease")

// renewPlatform renews c's lease every third of lease until ctx ends, and
// ends the conversion with errLeaseLost once the lease is another's. A
// failed renewal is tried again at the next tick; the lease outlasts two
// of them.
func (i *Images) renewPlatform(ctx context.Context, lost context.CancelCauseFunc, c platformConversion, lease time.Duration) {
	ticker := time.NewTicker(lease / 3)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		n, err := i.queries.RenewPlatformImage(ctx, RenewPlatformImageParams{
			Reference: c.reference, Architecture: c.architecture, Token: c.token, LeaseSeconds: lease.Seconds(),
		})
		if err == nil && n == 0 {
			lost(errLeaseLost)
			return
		}
	}
}

// finish records mirror as the converted copy under c's token, in tx, and
// announces it.
func (c platformConversion) finish(ctx context.Context, tx pgx.Tx, mirror string) error {
	if err := New(tx).FinishPlatformImage(ctx, FinishPlatformImageParams{
		Reference: c.reference, Architecture: c.architecture, Token: c.token, Mirror: mirror,
	}); err != nil {
		return fmt.Errorf("record platform image: %w", err)
	}
	return database.Notify(ctx, tx, database.ChannelImageBuild, PlatformConverted)
}

// convertPlatform copies source's image for c's architecture into the
// platform registry, converts and uploads each layer no shared pair holds
// yet, and records the copy's layers. The uploads are keyed by c's token
// as a build's are by its container.
func (i *Images) convertPlatform(ctx context.Context, source name.Digest, c platformConversion) (err error) {
	sourceAuth, _, err := i.registryLogin(ctx, source.RegistryStr())
	if err != nil {
		return err
	}
	img, err := remote.Image(source, remote.WithContext(ctx), remote.WithAuth(sourceAuth.authenticator()), remote.WithTransport(i.resolver.transport),
		remote.WithPlatform(v1.Platform{OS: "linux", Architecture: c.architecture}))
	if err != nil {
		return registryError(c.reference, err)
	}
	// The copy has the same manifest, so these are its layers too.
	layers, err := imageLayers(c.reference, img)
	var rejected *InvalidError
	if errors.As(err, &rejected) {
		return &ConversionError{Reason: rejected.Reason}
	}
	if err != nil {
		return err
	}
	var size int64
	for _, l := range layers {
		size += l.size
	}
	if size > maxPlatformImageBytes {
		return &ConversionError{Reason: fmt.Sprintf("%s has %d bytes of layers, more than %d", c.reference, size, int64(maxPlatformImageBytes))}
	}
	digest, err := img.Digest()
	if err != nil {
		return fmt.Errorf("digest %s: %w", c.reference, err)
	}
	repository := i.config.Repository + "/platform/" + strings.ReplaceAll(hostOf(source.Context()), ":", "-") + "/" + source.RepositoryStr()
	mirror := i.config.Registry + "/" + repository + "@" + digest.String()
	auth, err := i.login.auth(ctx)
	if err != nil {
		return err
	}
	var options []name.Option
	if i.config.Insecure {
		options = append(options, name.Insecure)
	}
	target, err := name.NewDigest(mirror, options...)
	if err != nil {
		return fmt.Errorf("mirror reference %q: %w", mirror, err)
	}
	if _, err := remote.Head(target, remote.WithContext(ctx), remote.WithAuth(auth.authenticator()), remote.WithTransport(i.resolver.transport)); err != nil {
		copyCtx, span := telemetry.Start(ctx, "images.copy_platform_image", trace.WithAttributes(attribute.Int64("lazycloud.bytes", size)))
		err = retryPlatform(copyCtx, func() error {
			return remote.Write(target, img, remote.WithContext(copyCtx), remote.WithAuth(auth.authenticator()), remote.WithTransport(i.resolver.transport))
		})
		telemetry.Fail(span, err)
		if err != nil {
			return fmt.Errorf("%w: copy %s into the platform registry: %w", ErrRegistryUnavailable, c.reference, err)
		}
	}
	pushed := publication{reference: mirror, owner: c.token, deadline: time.Now().Add(platformConversionTimeout), layers: layers}
	// What the conversion's uploads stored and no pair recorded is deleted
	// by the sweep once their URLs lapse.
	defer func() { err = errors.Join(err, i.endUploads(context.WithoutCancel(ctx), c.token)) }()
	offers, err := i.platformRound(ctx, c, pushed, nil, nil)
	if err != nil || len(offers) == 0 {
		return err
	}
	dir, err := os.MkdirTemp("", "lazycloud-platform-")
	if err != nil {
		return fmt.Errorf("%w: create conversion directory: %w", ErrServerDisk, err)
	}
	defer func() { _ = os.RemoveAll(dir) }()
	blobBytes := make(map[string]int64, len(layers))
	for _, l := range layers {
		blobBytes[l.blob] = l.size
	}
	trace.SpanFromContext(ctx).SetAttributes(attribute.Int("lazycloud.layers", len(layers)), attribute.Int("lazycloud.layers_converted", len(offers)))
	files, err := i.convertPlatformLayers(ctx, target, auth, dir, offers, blobBytes)
	if err != nil {
		return err
	}
	sizes := make([]ConvertedLayer, 0, len(files))
	for blob, f := range files {
		sizes = append(sizes, ConvertedLayer{Blob: blob, DataBytes: f.dataBytes, IndexBytes: int64(len(f.index))})
	}
	if offers, err = i.platformRound(ctx, c, pushed, nil, sizes); err != nil || len(offers) == 0 {
		return err
	}
	uploads, err := i.presignUploads(ctx, offers)
	if err != nil {
		return err
	}
	uploaded, err := i.uploadPlatformLayers(ctx, uploads, files)
	if err != nil {
		return err
	}
	converted, failure, err := i.checkConversions(ctx, pushed, uploaded)
	if err != nil {
		return err
	}
	if failure != "" {
		return &ConversionError{Reason: failure}
	}
	publishCtx, publish := telemetry.Start(ctx, "images.publish_platform_image")
	offers, err = i.platformRound(publishCtx, c, pushed, converted, nil)
	telemetry.Fail(publish, err)
	if err != nil {
		return err
	}
	if len(offers) > 0 {
		return fmt.Errorf("%d layers of %s are still not converted", len(offers), c.reference)
	}
	return nil
}

// platformRound records the checked conversions and the reported sizes of
// pushed's layers, as a build's completion does, and returns the uploads
// still missing. With none, the reference's layers are recorded and c
// finishes in the same transaction.
func (i *Images) platformRound(ctx context.Context, c platformConversion, pushed publication, converted []RecordLayerParams, sizes []ConvertedLayer) ([]offer, error) {
	var offers []offer
	var failure string
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		var err error
		offers, failure, err = i.recordLayers(ctx, i.queries.WithTx(tx), pushed, converted, sizes)
		if err != nil || failure != "" || len(offers) > 0 {
			return err
		}
		return c.finish(ctx, tx, pushed.reference)
	})
	if err != nil {
		return nil, fmt.Errorf("record platform image layers: %w", err)
	}
	if failure != "" {
		return nil, &ConversionError{Reason: failure}
	}
	return offers, nil
}

// platformLayer is a layer the server converted: its data in a file under
// the conversion's directory, and its index.
type platformLayer struct {
	data      string
	dataBytes int64
	index     []byte
}

// convertPlatformLayers converts the layers offers name, read from image's
// repository, a few at a time.
func (i *Images) convertPlatformLayers(ctx context.Context, image name.Digest, auth *Auth, dir string, offers []offer, blobBytes map[string]int64) (map[string]*platformLayer, error) {
	out := make([]*platformLayer, len(offers))
	g, ctx := errgroup.WithContext(ctx)
	g.SetLimit(platformLayerParallelism)
	for n, o := range offers {
		g.Go(func() error {
			blob := image.Context().Digest(o.blob)
			layerCtx, span := telemetry.Start(ctx, "images.convert_layer", trace.WithAttributes(
				attribute.String(telemetry.AttrLayer, o.diffID), attribute.Int64("lazycloud.bytes", blobBytes[o.blob])))
			err := retryPlatform(layerCtx, func() error {
				l, err := i.convertPlatformLayer(layerCtx, blob, auth, dir, o.diffID, blobBytes[o.blob])
				out[n] = l
				return err
			})
			telemetry.Fail(span, err)
			if err != nil {
				return fmt.Errorf("convert layer %s: %w", o.blob, err)
			}
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err //nolint:wrapcheck // Each conversion's error names its layer.
	}
	files := make(map[string]*platformLayer, len(offers))
	for n, o := range offers {
		files[o.blob] = out[n]
	}
	return files, nil
}

// convertPlatformLayer downloads blob, of size bytes, and converts it once
// its digest checks. A download cut short is the registry's failure; only
// a blob that holds its digest's bytes can fail as content.
func (i *Images) convertPlatformLayer(ctx context.Context, blob name.Digest, auth *Auth, dir, diffID string, size int64) (*platformLayer, error) {
	fetchCtx, fetch := telemetry.Start(ctx, "images.download_layer")
	path, mediaType, err := i.fetchPlatformBlob(fetchCtx, blob, auth, dir, size)
	telemetry.Fail(fetch, err)
	if err != nil {
		return nil, err
	}
	ctx, convert := telemetry.Start(ctx, "images.convert_layer_data")
	defer convert.End()
	defer func() { _ = os.Remove(path) }()
	layer, err := tarball.LayerFromFile(path, tarball.WithMediaType(mediaType))
	if err != nil {
		return nil, fmt.Errorf("open layer %s: %w", blob.DigestStr(), err)
	}
	uncompressed, err := layer.Uncompressed()
	if err != nil {
		return nil, fmt.Errorf("open layer %s: %w", blob.DigestStr(), err)
	}
	defer func() { _ = uncompressed.Close() }()
	data, err := os.CreateTemp(dir, "layer-*.data")
	if err != nil {
		return nil, fmt.Errorf("%w: create layer data file: %w", ErrServerDisk, err)
	}
	defer func() { _ = data.Close() }()
	ix, index, err := imagefs.ConvertLayer(ctx, uncompressed, data, imagefs.Digest(diffID))
	if errors.Is(err, imagefs.ErrInvalidLayer) {
		return nil, &ConversionError{Reason: fmt.Sprintf("layer %s: %v", blob.DigestStr(), err)}
	}
	if err != nil {
		return nil, fmt.Errorf("convert layer: %w", err)
	}
	return &platformLayer{data: data.Name(), dataBytes: ix.DataSize, index: index}, nil
}

// fetchPlatformBlob downloads blob, of size bytes, into a file under dir
// and returns its path and media type once the bytes hold the digest.
func (i *Images) fetchPlatformBlob(ctx context.Context, blob name.Digest, auth *Auth, dir string, size int64) (string, types.MediaType, error) {
	layer, err := remote.Layer(blob, remote.WithContext(ctx), remote.WithAuth(auth.authenticator()), remote.WithTransport(i.resolver.transport))
	if err != nil {
		return "", "", fmt.Errorf("%w: read layer %s: %w", ErrRegistryUnavailable, blob.DigestStr(), err)
	}
	mediaType, err := layer.MediaType()
	if err != nil {
		return "", "", fmt.Errorf("%w: read layer %s: %w", ErrRegistryUnavailable, blob.DigestStr(), err)
	}
	body, err := layer.Compressed()
	if err != nil {
		return "", "", fmt.Errorf("%w: read layer %s: %w", ErrRegistryUnavailable, blob.DigestStr(), err)
	}
	defer func() { _ = body.Close() }()
	file, err := os.CreateTemp(dir, "blob-*")
	if err != nil {
		return "", "", fmt.Errorf("%w: create layer blob file: %w", ErrServerDisk, err)
	}
	hash := sha256.New()
	n, err := io.Copy(io.MultiWriter(file, hash), io.LimitReader(body, size+1))
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	got := "sha256:" + hex.EncodeToString(hash.Sum(nil))
	switch {
	case err != nil:
		err = fmt.Errorf("%w: download layer %s: %w", ErrRegistryUnavailable, blob.DigestStr(), err)
	case n != size || got != blob.DigestStr():
		err = fmt.Errorf("%w: the registry sent %d bytes holding %s for layer %s of %d bytes", ErrRegistryUnavailable, n, got, blob.DigestStr(), size)
	}
	if err != nil {
		_ = os.Remove(file.Name())
		return "", "", err
	}
	return file.Name(), mediaType, nil
}

// uploadPlatformLayers PUTs each layer's data parts, then its index, to
// the URLs uploads carry, and returns the parts' ETags.
func (i *Images) uploadPlatformLayers(ctx context.Context, uploads []LayerUpload, files map[string]*platformLayer) ([]UploadedLayer, error) {
	for _, u := range uploads {
		if files[u.Blob] == nil || u.Index == "" {
			return nil, fmt.Errorf("layer %s has no converted data or no upload", u.Blob)
		}
	}
	out := make([]UploadedLayer, len(uploads))
	g, ctx := errgroup.WithContext(ctx)
	g.SetLimit(platformLayerParallelism)
	for n, u := range uploads {
		l := files[u.Blob]
		g.Go(func() error {
			uploadCtx, span := telemetry.Start(ctx, "images.upload_layer", trace.WithAttributes(
				attribute.String(telemetry.AttrLayer, u.DiffID), attribute.Int64("lazycloud.bytes", l.dataBytes+int64(len(l.index)))))
			etags, err := i.uploadPlatformLayer(uploadCtx, u, l)
			telemetry.Fail(span, err)
			if err != nil {
				return fmt.Errorf("upload layer %s: %w", u.Blob, err)
			}
			out[n] = UploadedLayer{Blob: u.Blob, ETags: etags}
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err //nolint:wrapcheck // Each upload's error names its layer.
	}
	return out, nil
}

func (i *Images) uploadPlatformLayer(ctx context.Context, u LayerUpload, l *platformLayer) ([]string, error) {
	data, err := os.Open(l.data)
	if err != nil {
		return nil, fmt.Errorf("open layer data: %w", err)
	}
	defer func() { _ = data.Close() }()
	return imagefs.UploadPair(ctx, i.transfer, data, l.dataBytes, l.index, u.DataParts, u.PartBytes, u.Index, retryPlatform) //nolint:wrapcheck // The caller names the layer.
}

// retryPlatform runs fn until it succeeds, fails for the image, the
// server's disk or the store's refusal, or uses its attempts.
func retryPlatform(ctx context.Context, fn func() error) error {
	final := func(err error) bool {
		var unconvertible *ConversionError
		return errors.As(err, &unconvertible) || errors.Is(err, ErrServerDisk)
	}
	return imagefs.Retry(ctx, platformTransferAttempts, platformTransferBackoff, final, fn) //nolint:wrapcheck // fn's error.
}
