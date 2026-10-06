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
	"golang.org/x/sync/errgroup"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/imagefs"
)

// Platform images are the images agents run on their own, such as the
// image builder and the volume mount image. A build runs in the builder,
// so no build can convert them; the server converts each itself, once per
// reference and host architecture across its replicas: it copies the image
// into the platform registry and converts the copy's layers into shared
// pairs through the same offers and uploads a build's host is given.
// Sessions ask only for the images package platformimages names.

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
}

// PlatformPulls returns how host pulls each of references, the platform
// images its agent named, for the host's architecture. A reference that
// is not a public image by digest is a Failure.
func (i *Images) PlatformPulls(ctx context.Context, host compute.HostID, references []string) ([]PlatformPull, error) {
	architecture, err := i.queries.HostArchitecture(ctx, uuid.UUID(host))
	if err != nil {
		return nil, fmt.Errorf("read host architecture: %w", err)
	}
	rows, err := i.queries.PlatformImagesOf(ctx, PlatformImagesOfParams{Refs: references, Architecture: architecture})
	if err != nil {
		return nil, fmt.Errorf("read platform images: %w", err)
	}
	byReference := make(map[string]PlatformImagesOfRow, len(rows))
	for _, row := range rows {
		byReference[row.Reference] = row
	}
	out := make([]PlatformPull, len(references))
	for n, reference := range references {
		out[n] = PlatformPull{Reference: reference, Architecture: architecture}
		if _, err := i.platformSource(reference); err != nil {
			out[n].Failure = err.Error()
			continue
		}
		row, ok := byReference[reference]
		switch {
		case !ok:
		case row.Converted:
			pull, err := i.platformPull(ctx, *row.Mirror, architecture)
			if err != nil {
				return nil, err
			}
			out[n].Pull = &pull
		case row.Failure != nil && !row.FailureTransient && row.FailedAt != nil && time.Since(*row.FailedAt) < platformFailureRetry:
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
	rows, err := i.queries.PlatformCopiesOf(ctx, mirrors)
	if err != nil {
		return nil, fmt.Errorf("read platform image copies: %w", err)
	}
	out := make([]PlatformPull, len(rows))
	for n, row := range rows {
		out[n] = PlatformPull{
			Reference: row.Reference, Architecture: row.Architecture,
			Pull: &Pull{Reference: row.Mirror, Platform: "linux/" + row.Architecture},
		}
	}
	return out, nil
}

// platformPull is how a host pulls mirror, the converted copy of a
// platform image.
func (i *Images) platformPull(ctx context.Context, mirror, architecture string) (Pull, error) {
	repository, _ := i.config.repositoryOf(mirror)
	auth, err := i.login.host(ctx, hostAccess{pull: []string{repository}}, time.Now().Add(platformPullWindow))
	if err != nil {
		return Pull{}, err
	}
	return Pull{Reference: mirror, Auth: auth, Platform: "linux/" + architecture}, nil
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

// ConvertPlatformImage converts reference, a platform image, for
// architecture unless it is converted, another replica's conversion holds
// its lease, or its last attempt failed within the retry period. The
// outcome is recorded and announced on ChannelImageBuild under
// PlatformConverted. A failure of the image is a ConversionError.
func (i *Images) ConvertPlatformImage(ctx context.Context, reference, architecture string) error {
	source, err := i.platformSource(reference)
	if err != nil {
		return err
	}
	token := uuid.New()
	// The lease names the invariant: one conversion of a reference and
	// architecture at a time. Its outcome is recorded only under the token,
	// and every record before that is idempotent, so an owner whose lease
	// lapsed changes nothing another depends on.
	recorded, err := i.queries.ClaimPlatformImage(ctx, ClaimPlatformImageParams{
		Reference: reference, Architecture: architecture, Token: token, LeaseSeconds: i.platformLease.Seconds(),
		TransientRetrySeconds: platformTransientRetry.Seconds(), FailureRetrySeconds: platformFailureRetry.Seconds(),
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("claim platform image: %w", err)
	}
	if recorded != nil {
		rows, err := i.queries.ReferenceLayers(ctx, *recorded)
		if err != nil {
			return fmt.Errorf("read image layers: %w", err)
		}
		if len(rows) > 0 {
			return i.finishPlatform(ctx, reference, architecture, token, *recorded)
		}
	}
	convertCtx, cancel := context.WithTimeout(ctx, platformConversionTimeout)
	defer cancel()
	renewCtx, lost := context.WithCancelCause(convertCtx)
	var renewals sync.WaitGroup
	renewals.Go(func() { i.renewPlatform(renewCtx, lost, reference, architecture, token) })
	err = i.convertPlatform(renewCtx, source, reference, architecture, token)
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
			Reference: reference, Architecture: architecture, Token: token, Failure: truncate(err.Error()), Transient: transient,
		}); err != nil {
			return fmt.Errorf("record platform image failure: %w", err)
		}
		return database.Notify(record, tx, database.ChannelImageBuild, PlatformConverted)
	})
	return errors.Join(fmt.Errorf("convert platform image %s: %w", reference, err), failed)
}

// errLeaseLost ends a conversion whose lease another owner took.
var errLeaseLost = errors.New("another owner took the conversion's lease")

// renewPlatform renews the lease of token's conversion every third of the
// lease until ctx ends, and ends the conversion with errLeaseLost once
// the lease is another's. A failed renewal is tried again at the next
// tick; the lease outlasts two of them.
func (i *Images) renewPlatform(ctx context.Context, lost context.CancelCauseFunc, reference, architecture string, token uuid.UUID) {
	ticker := time.NewTicker(i.platformLease / 3)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		n, err := i.queries.RenewPlatformImage(ctx, RenewPlatformImageParams{
			Reference: reference, Architecture: architecture, Token: token, LeaseSeconds: i.platformLease.Seconds(),
		})
		if err == nil && n == 0 {
			lost(errLeaseLost)
			return
		}
	}
}

// finishPlatform records mirror as the converted copy of reference under
// token and announces it.
func (i *Images) finishPlatform(ctx context.Context, reference, architecture string, token uuid.UUID, mirror string) error {
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		return i.finishPlatformTx(ctx, i.queries.WithTx(tx), tx, reference, architecture, token, mirror)
	})
	if err != nil {
		return fmt.Errorf("finish platform image: %w", err)
	}
	return nil
}

func (i *Images) finishPlatformTx(ctx context.Context, q *Queries, tx pgx.Tx, reference, architecture string, token uuid.UUID, mirror string) error {
	if _, err := q.FinishPlatformImage(ctx, FinishPlatformImageParams{Reference: reference, Architecture: architecture, Token: token, Mirror: mirror}); err != nil {
		return fmt.Errorf("record platform image: %w", err)
	}
	return database.Notify(ctx, tx, database.ChannelImageBuild, PlatformConverted)
}

// convertPlatform copies source's image for architecture into the platform
// registry, converts and uploads each layer no shared pair holds yet, and
// records the copy's layers. The uploads are keyed by token as a build's
// are by its container.
func (i *Images) convertPlatform(ctx context.Context, source name.Digest, reference, architecture string, token uuid.UUID) (err error) {
	var sourceAuth *Auth
	if source.RegistryStr() == i.config.Registry {
		if sourceAuth, err = i.login.auth(ctx); err != nil {
			return err
		}
	}
	img, err := remote.Image(source, remote.WithContext(ctx), remote.WithAuth(sourceAuth.authenticator()), remote.WithTransport(i.resolver.transport),
		remote.WithPlatform(v1.Platform{OS: "linux", Architecture: architecture}))
	if err != nil {
		return registryError(reference, err)
	}
	manifest, err := img.Manifest()
	if err != nil {
		return fmt.Errorf("%w: read the manifest of %s: %w", ErrRegistryUnavailable, reference, err)
	}
	var size int64
	for _, l := range manifest.Layers {
		size += l.Size
	}
	if size > maxPlatformImageBytes {
		return &ConversionError{Reason: fmt.Sprintf("%s has %d bytes of layers, more than %d", reference, size, int64(maxPlatformImageBytes))}
	}
	digest, err := img.Digest()
	if err != nil {
		return fmt.Errorf("digest %s: %w", reference, err)
	}
	host := source.RegistryStr()
	if host == name.DefaultRegistry {
		host = "docker.io"
	}
	repository := i.config.Repository + "/platform/" + strings.ReplaceAll(host, ":", "-") + "/" + source.RepositoryStr()
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
		err = retryPlatform(ctx, func() error {
			return remote.Write(target, img, remote.WithContext(ctx), remote.WithAuth(auth.authenticator()), remote.WithTransport(i.resolver.transport))
		})
		if err != nil {
			return fmt.Errorf("%w: copy %s into the platform registry: %w", ErrRegistryUnavailable, reference, err)
		}
	}
	pushed := publication{
		reference: mirror, target: buildTarget{repository: repository}, container: token,
		deadline: time.Now().Add(platformConversionTimeout),
	}
	pushed.layers, err = i.resolver.layers(ctx, mirror, auth, i.config.Insecure, "linux/"+architecture)
	var rejected *InvalidError
	if errors.As(err, &rejected) {
		return &ConversionError{Reason: rejected.Reason}
	}
	if err != nil {
		return err
	}
	// What the conversion's uploads stored and no pair recorded is deleted
	// by the sweep once their URLs lapse.
	defer func() { err = errors.Join(err, i.endUploads(context.WithoutCancel(ctx), token)) }()
	finish := func(q *Queries, tx pgx.Tx) error {
		return i.finishPlatformTx(ctx, q, tx, reference, architecture, token, mirror)
	}
	offers, err := i.platformRound(ctx, pushed, nil, nil, finish)
	if err != nil || len(offers) == 0 {
		return err
	}
	dir, err := os.MkdirTemp("", "lazycloud-platform-")
	if err != nil {
		return fmt.Errorf("%w: create conversion directory: %w", ErrServerDisk, err)
	}
	defer func() { _ = os.RemoveAll(dir) }()
	blobBytes := make(map[string]int64, len(pushed.layers))
	for _, l := range pushed.layers {
		blobBytes[l.blob] = l.size
	}
	files, err := i.convertPlatformLayers(ctx, target, auth, dir, offers, blobBytes)
	if err != nil {
		return err
	}
	sizes := make([]ConvertedLayer, 0, len(files))
	for blob, f := range files {
		sizes = append(sizes, ConvertedLayer{Blob: blob, DataBytes: f.dataBytes, IndexBytes: int64(len(f.index))})
	}
	if offers, err = i.platformRound(ctx, pushed, nil, sizes, finish); err != nil || len(offers) == 0 {
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
	if offers, err = i.platformRound(ctx, pushed, converted, nil, finish); err != nil {
		return err
	}
	if len(offers) > 0 {
		return fmt.Errorf("%d layers of %s are still not converted", len(offers), reference)
	}
	return nil
}

// platformRound records the checked conversions and the reported sizes of
// pushed's layers, as a build's completion does, and returns the uploads
// still missing. With none, the reference's layers are recorded and
// finish records the conversion in the same transaction.
func (i *Images) platformRound(ctx context.Context, pushed publication, converted []conversion, sizes []ConvertedLayer, finish func(*Queries, pgx.Tx) error) ([]offer, error) {
	var offers []offer
	var failure string
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		var err error
		offers, failure, err = i.recordLayers(ctx, q, pushed, converted, sizes)
		if err != nil || failure != "" || len(offers) > 0 {
			return err
		}
		return finish(q, tx)
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
			err := retryPlatform(ctx, func() error {
				l, err := i.convertPlatformLayer(ctx, blob, auth, dir, o.diffID, blobBytes[o.blob])
				out[n] = l
				return err
			})
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
	path, mediaType, err := i.fetchPlatformBlob(ctx, blob, auth, dir, size)
	if err != nil {
		return nil, err
	}
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
			etags, err := i.uploadPlatformLayer(ctx, u, l)
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
