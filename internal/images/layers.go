package images

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"slices"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// uploadURLLifetime is how long a host has to convert and upload the
	// layers one completion names; it asks again for fresh URLs after.
	uploadURLLifetime = time.Hour
	// uploadGrace keeps an upload's objects past its build's deadline, until
	// no late write to them can follow.
	uploadGrace = time.Hour
	// layerGrace is how long a pair stays after the last live image stopped
	// using it, longer than any build that found it converted takes to
	// publish.
	layerGrace      = 24 * time.Hour
	layerSweepBatch = 100
)

// ErrNotConverted means a reference has no layer rows: no build converted
// it, or a layer it used was retired.
var ErrNotConverted = errors.New("the image has no converted layers")

// LayerURLs reads one converted layer: its index and data objects.
type LayerURLs struct {
	DiffID      imagefs.Digest
	Index, Data string
	ExpiresAt   time.Time
}

// LayerReads is how a host reads the layers of one reference.
type LayerReads struct {
	Layers []LayerURLs
	// Unconfirmed is the host's region when its copy of the layer bucket is
	// not yet confirmed to hold every layer; those layers read from the
	// layer bucket until ConfirmReplicas confirms them there.
	Unconfirmed string
}

// ReplicasConfirmed is the ChannelImageBuild key that announces a
// confirmation of layers in region's copy of the layer bucket, after which
// grants that read those layers from the layer bucket may read the copy.
func ReplicasConfirmed(region string) string { return "layer-replicas:" + region }

// LayerReadURLs is LayerReadURLsOf for one reference: a reference without
// converted layers is ErrNotConverted.
func (i *Images) LayerReadURLs(ctx context.Context, reference string, host compute.HostID, ttl time.Duration) (LayerReads, error) {
	reads, err := i.LayerReadURLsOf(ctx, []string{reference}, host, ttl)
	if err != nil {
		return LayerReads{}, err
	}
	out, ok := reads[reference]
	if !ok {
		return LayerReads{}, fmt.Errorf("%s: %w", reference, ErrNotConverted)
	}
	return out, nil
}

// LayerReadURLsOf presigns GET URLs, valid for up to ttl, for every layer of
// each of references, images by digest, in layer order, for host: from its
// region's copy of the layer bucket where that copy is confirmed to hold
// the layer, and from the layer bucket otherwise. A reference is converted
// as a whole or not at all, so a reference with an unconverted layer is
// left out. It reads every reference's layers in one query.
//
// Layers belong to the reference rather than the image id: a workspace's
// rebuild gives an image a new reference while releases that pinned the old
// one keep running it.
func (i *Images) LayerReadURLsOf(ctx context.Context, references []string, host compute.HostID, ttl time.Duration) (out map[string]LayerReads, err error) {
	ctx, span := telemetry.Start(ctx, "images.grant_layers", trace.WithAttributes(attribute.String(telemetry.AttrImage, strings.Join(references, " "))))
	layers := 0
	defer func() {
		span.SetAttributes(attribute.Int("lazycloud.layers", layers))
		telemetry.Fail(span, err)
	}()
	rows, err := i.queries.LayerReadsFor(ctx, LayerReadsForParams{Refs: references, Host: uuid.UUID(host)})
	if err != nil {
		return nil, fmt.Errorf("read image layers: %w", err)
	}
	out = make(map[string]LayerReads, len(references))
	for _, row := range rows {
		reads := out[row.Reference]
		region := ""
		if i.storage.HasLayerReplica(row.Region) {
			if row.Replicated {
				region = row.Region
			} else {
				reads.Unconfirmed = row.Region
				span.SetAttributes(attribute.String("lazycloud.unconfirmed_region", row.Region))
			}
		}
		index, expires, err := i.storage.LayerReadURL(ctx, row.ID, storage.LayerIndex, region, ttl)
		if err != nil {
			return nil, err
		}
		data, dataExpires, err := i.storage.LayerReadURL(ctx, row.ID, storage.LayerData, region, ttl)
		if err != nil {
			return nil, err
		}
		if dataExpires.Before(expires) {
			expires = dataExpires
		}
		reads.Layers = append(reads.Layers, LayerURLs{DiffID: imagefs.Digest(row.DiffID), Index: index, Data: data, ExpiresAt: expires})
		out[row.Reference] = reads
		layers++
	}
	return out, nil
}

// ReplicaCheck is what one ConfirmReplicas call did.
type ReplicaCheck struct {
	// Checked counts the layers this call checked.
	Checked int
	// Pending means some layer of the reference is still not confirmed in
	// the region's copy.
	Pending bool
}

// ConfirmReplicas checks region's copy of the layer bucket for the layers
// of reference it is not confirmed to hold, records those it holds and
// announces them under ReplicasConfirmed. A check is claimed per layer and
// region, so concurrent calls on any server check each layer once, and a
// layer found missing is checked again only after recheck. A failed check
// leaves the others' confirmations recorded.
func (i *Images) ConfirmReplicas(ctx context.Context, reference, region string, recheck time.Duration) (ReplicaCheck, error) {
	if !i.storage.HasLayerReplica(region) {
		return ReplicaCheck{}, nil
	}
	ids, err := i.queries.ClaimReplicaChecks(ctx, ClaimReplicaChecksParams{Reference: reference, Region: region, RetrySeconds: recheck.Seconds()})
	if err != nil {
		return ReplicaCheck{}, fmt.Errorf("claim layer replica checks: %w", err)
	}
	out := ReplicaCheck{Checked: len(ids)}
	defer func() {
		trace.SpanFromContext(ctx).AddEvent("checked", trace.WithAttributes(
			attribute.Int("lazycloud.layers", out.Checked), attribute.Bool("lazycloud.pending", out.Pending)))
	}()
	var errs []error
	held := make([]uuid.UUID, 0, len(ids))
	for _, id := range ids {
		ok, err := i.storage.LayerReplicated(ctx, id, region)
		if err != nil {
			errs = append(errs, err)
			continue
		}
		if ok {
			held = append(held, id)
		}
	}
	if len(held) > 0 {
		err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
			n, err := i.queries.WithTx(tx).ConfirmReplicas(ctx, ConfirmReplicasParams{Region: region, LayerIds: held})
			if err != nil || n == 0 {
				return err //nolint:wrapcheck // Wrapped below.
			}
			return database.Notify(ctx, tx, database.ChannelImageBuild, ReplicasConfirmed(region))
		})
		if err != nil {
			errs = append(errs, fmt.Errorf("record layer replicas: %w", err))
		}
	}
	if out.Pending, err = i.queries.ReplicasPending(ctx, ReplicasPendingParams{Reference: reference, Region: region}); err != nil {
		errs = append(errs, fmt.Errorf("read layer replicas: %w", err))
		out.Pending = true
	}
	return out, errors.Join(errs...)
}

// RecordUses records that references were published or sent to hosts in
// starts, which keeps their layers live through the grace period.
func (i *Images) RecordUses(ctx context.Context, references []string) error {
	if len(references) == 0 {
		return nil
	}
	if err := i.queries.RecordUses(ctx, references); err != nil {
		return fmt.Errorf("record image uses: %w", err)
	}
	return nil
}

// LayerUpload is a layer of a pushed image for its host to convert: read
// Blob from the push repository, convert it and check the index carries
// DiffID. Without URLs the host reports the converted sizes; with them it
// PUTs each data part, PartBytes each but the last, then the index.
type LayerUpload struct {
	Blob, DiffID string
	Index        string
	DataParts    []string
	PartBytes    int64
}

// ConvertedLayer is a layer a host converted, with the sizes of its pair.
type ConvertedLayer struct {
	Blob                  string
	DataBytes, IndexBytes int64
}

// UploadedLayer is a layer whose pair a host uploaded: the ETags of the
// data parts, in order.
type UploadedLayer struct {
	Blob  string
	ETags []string
}

// publication is a pushed image whose layers a build container, or the
// server conversion leased as owner, converts.
type publication struct {
	reference string
	// scope is the workspace a scoped build's pairs serve alone: its content
	// is the workspace's, and a customer's host could upload anything. Pairs
	// a platform host or the server converted serve everyone, since the
	// blob they were read from is the registry's.
	scope    *uuid.UUID
	owner    uuid.UUID
	deadline time.Time
	layers   []registryLayer
}

// offer is an upload a completion hands its host. start holds the sizes
// the host reported when they differ from those of the data upload started
// so far, which a new upload replaces.
type offer struct {
	OfferUploadRow
	blob, diffID string
	start        *ConvertedLayer
}

// checkConversions completes the data uploads of the pairs the host
// reported uploaded for pushed and reads what the store holds. A pair not
// stored yet is left out and offered again. A pair that does not hold its
// layer is a failure of the build, returned as its reason.
func (i *Images) checkConversions(ctx context.Context, pushed publication, reported []UploadedLayer) ([]RecordLayerParams, string, error) {
	if len(reported) == 0 {
		return nil, "", nil
	}
	diffIDs := map[string]string{}
	for _, l := range pushed.layers {
		diffIDs[l.blob] = l.diffID
	}
	etags := map[string][]string{}
	for _, r := range reported {
		etags[r.Blob] = r.ETags
	}
	uploads, err := i.queries.UploadsOf(ctx, UploadsOfParams{ContainerID: &pushed.owner, Blobs: slices.Collect(maps.Keys(etags))})
	if err != nil {
		return nil, "", fmt.Errorf("read layer uploads: %w", err)
	}
	var out []RecordLayerParams
	for _, u := range uploads {
		want, ok := diffIDs[u.BlobDigest]
		if !ok || u.UploadID == nil {
			continue
		}
		parts := etags[u.BlobDigest]
		if n := storage.LayerDataParts(*u.DataBytes); int64(len(parts)) != n {
			return nil, fmt.Sprintf("layer %s was uploaded in %d parts, not %d", u.BlobDigest, len(parts), n), nil
		}
		err := i.storage.CompleteLayerUpload(ctx, u.ID, *u.UploadID, parts)
		var refused *storage.InvalidError
		switch {
		case errors.As(err, &refused):
			return nil, fmt.Sprintf("layer %s: %s", u.BlobDigest, refused.Reason), nil
		case err != nil && !errors.Is(err, storage.ErrNotFound):
			return nil, "", err
		}
		// A completed upload is gone, so a repeated report finds the object.
		size, err := i.storage.LayerSize(ctx, u.ID, storage.LayerData)
		var raw []byte
		if err == nil {
			raw, err = i.storage.ReadLayer(ctx, u.ID, storage.LayerIndex, *u.IndexBytes)
		}
		if errors.Is(err, storage.ErrNotFound) {
			continue
		}
		if err != nil {
			return nil, "", err
		}
		ix, err := imagefs.Unmarshal(raw)
		switch {
		case err != nil:
			return nil, fmt.Sprintf("the converted index of layer %s is unreadable: %v", u.BlobDigest, err), nil
		case string(ix.Layer) != want:
			return nil, fmt.Sprintf("layer %s holds %s, but the image config names %s", u.BlobDigest, ix.Layer, want), nil
		case size != *u.DataBytes || size != ix.DataSize:
			return nil, fmt.Sprintf("the converted data of layer %s has %d bytes, its index %d", u.BlobDigest, size, ix.DataSize), nil
		}
		out = append(out, RecordLayerParams{
			ID: u.ID, BlobDigest: u.BlobDigest, DiffID: want, WorkspaceID: pushed.scope,
			Frames: int32(len(ix.Frames)), //nolint:gosec // imagefs bounds it below 2^31.
		})
	}
	return out, "", nil
}

// maxDataBytes bounds the data object a layer whose blob has blobBytes may
// convert to: compressed again, with the zero padding that keeps small files
// in one frame.
func maxDataBytes(blobBytes int64) int64 { return 2*blobBytes + 64<<20 }

// recordLayers records the checked conversions and, when every layer of
// pushed has a pair it may use, the reference's layers. Otherwise it returns
// the uploads its host makes next, sized by what the host reported. A pair
// whose diff_id differs from the config's, or a size no layer of its blob
// converts to, fails the build with the returned reason.
func (i *Images) recordLayers(ctx context.Context, q *Queries, pushed publication, converted []RecordLayerParams, sizes []ConvertedLayer) ([]offer, string, error) {
	// Inserts first and in blob order, so concurrent completions wait on
	// each other's pairs in one order.
	slices.SortFunc(converted, func(a, b RecordLayerParams) int { return cmp.Compare(a.BlobDigest, b.BlobDigest) })
	for _, c := range converted {
		n, err := q.RecordLayer(ctx, c)
		if err == nil && n > 0 {
			err = q.ClaimUpload(ctx, c.ID)
		} else if err == nil {
			err = q.AbandonUpload(ctx, AbandonUploadParams{ID: c.ID, UrlSeconds: uploadURLLifetime.Seconds()})
		}
		if err != nil {
			return nil, "", fmt.Errorf("record layer: %w", err)
		}
	}
	blobs := make([]string, len(pushed.layers))
	for n, l := range pushed.layers {
		blobs[n] = l.blob
	}
	rows, err := q.UsableLayers(ctx, UsableLayersParams{Blobs: blobs, WorkspaceID: pushed.scope})
	if err != nil {
		return nil, "", fmt.Errorf("read converted layers: %w", err)
	}
	usable := map[string]UsableLayersRow{}
	for _, row := range rows {
		if have, ok := usable[row.BlobDigest]; !ok || (row.Shared && !have.Shared) {
			usable[row.BlobDigest] = row
		}
	}
	reported := map[string]ConvertedLayer{}
	for _, c := range sizes {
		reported[c.Blob] = c
	}
	ids := make([]uuid.UUID, len(pushed.layers))
	var offers []offer
	for n, l := range pushed.layers {
		if row, ok := usable[l.blob]; ok {
			if row.DiffID != l.diffID {
				return nil, fmt.Sprintf("layer %d (%s) holds %s, but the image config names %s", n, l.blob, row.DiffID, l.diffID), nil
			}
			ids[n] = row.ID
			continue
		}
		if slices.ContainsFunc(offers, func(o offer) bool { return o.blob == l.blob }) {
			continue
		}
		row, err := q.OfferUpload(ctx, OfferUploadParams{ContainerID: &pushed.owner, BlobDigest: l.blob, ExpiresAt: pushed.deadline.Add(uploadGrace)})
		if err != nil {
			return nil, "", fmt.Errorf("offer layer upload: %w", err)
		}
		o := offer{OfferUploadRow: row, blob: l.blob, diffID: l.diffID}
		if c, ok := reported[l.blob]; ok && (row.UploadID == nil || c.DataBytes != *row.DataBytes || c.IndexBytes != *row.IndexBytes) {
			if c.DataBytes < 0 || c.DataBytes > maxDataBytes(l.size) || c.IndexBytes <= 0 || c.IndexBytes > imagefs.MaxIndexSize {
				return nil, fmt.Sprintf("layer %s converted to %d data and %d index bytes, more than a %d byte layer needs",
					l.blob, c.DataBytes, c.IndexBytes, l.size), nil
			}
			o.start = &c
		}
		offers = append(offers, o)
	}
	if len(offers) > 0 {
		return offers, "", nil
	}
	if err := q.RecordReference(ctx, RecordReferenceParams{Reference: pushed.reference, LayerIds: ids}); err != nil {
		return nil, "", fmt.Errorf("record image layers: %w", err)
	}
	return nil, "", nil
}

// presignUploads starts the data uploads of offers whose host reported new
// sizes, replacing the one started before, and signs the URLs of every
// offer with a started upload. The others go out without URLs, to be
// converted and sized first.
func (i *Images) presignUploads(ctx context.Context, offers []offer) ([]LayerUpload, error) {
	out := make([]LayerUpload, len(offers))
	for n, o := range offers {
		out[n] = LayerUpload{Blob: o.blob, DiffID: o.diffID}
		if o.start != nil {
			uploadID, err := i.storage.CreateLayerUpload(ctx, o.ID, o.start.DataBytes)
			if err != nil {
				return nil, err
			}
			started, err := i.queries.StartUpload(ctx, StartUploadParams{
				ID: o.ID, Previous: o.UploadID, UploadID: uploadID, DataBytes: o.start.DataBytes, IndexBytes: o.start.IndexBytes,
			})
			if err == nil && started == 0 {
				err = errors.New("another report started this layer's upload; report again")
			}
			if err != nil {
				_ = i.storage.AbortLayerUpload(ctx, o.ID, uploadID)
				return nil, fmt.Errorf("start layer upload: %w", err)
			}
			if o.UploadID != nil {
				if err := i.storage.AbortLayerUpload(ctx, o.ID, *o.UploadID); err != nil {
					return nil, err
				}
			}
			o.UploadID, o.DataBytes, o.IndexBytes = &uploadID, &o.start.DataBytes, &o.start.IndexBytes
		}
		if o.UploadID == nil {
			continue
		}
		urls, err := i.storage.PresignLayerUpload(ctx, o.ID, *o.UploadID, *o.DataBytes, *o.IndexBytes, uploadURLLifetime)
		if err != nil {
			return nil, err
		}
		out[n].Index, out[n].DataParts, out[n].PartBytes = urls.Index, urls.DataParts, storage.LayerPartBytes
	}
	return out, nil
}

// endUploads ends the uploads of a build container whose build finished and
// aborts their data uploads. The sweep deletes what they stored, and aborts
// again any upload whose abort failed here.
func (i *Images) endUploads(ctx context.Context, container uuid.UUID) error {
	rows, err := i.queries.EndContainerUploads(ctx, EndContainerUploadsParams{ContainerID: &container, UrlSeconds: uploadURLLifetime.Seconds()})
	if err != nil {
		return fmt.Errorf("end layer uploads: %w", err)
	}
	var errs []error
	for _, row := range rows {
		if row.UploadID != nil {
			errs = append(errs, i.storage.AbortLayerUpload(ctx, row.ID, *row.UploadID))
		}
	}
	return errors.Join(errs...)
}

// SweepLayers runs one bounded pass over converted layers. It starts the
// grace period of pairs no live reference uses, retires those unused
// through it, and deletes the objects of retired pairs and expired uploads.
// Each pair commits on its own, so one failure keeps the others' progress;
// replicas may sweep at once, since every step is conditional on the row.
func (i *Images) SweepLayers(ctx context.Context, logger *slog.Logger) error {
	grace := layerGrace.Seconds()
	if err := i.queries.PurgeUses(ctx, grace); err != nil {
		return fmt.Errorf("purge image uses: %w", err)
	}
	if err := i.queries.MarkUnreferencedLayers(ctx, grace); err != nil {
		return fmt.Errorf("mark unreferenced layers: %w", err)
	}
	ids, err := i.queries.UnreferencedLayers(ctx, UnreferencedLayersParams{GraceSeconds: grace, BatchSize: layerSweepBatch})
	if err != nil {
		return fmt.Errorf("read unreferenced layers: %w", err)
	}
	for _, id := range ids {
		if err := i.queries.RetireLayer(ctx, RetireLayerParams{ID: id, GraceSeconds: grace}); err != nil {
			logger.WarnContext(ctx, "retiring an unreferenced layer failed", "layer", id.String(), "error", err)
		}
	}
	rows, err := i.queries.ExpiredUploads(ctx, layerSweepBatch)
	if err != nil {
		return fmt.Errorf("read expired layer uploads: %w", err)
	}
	expired := make([]uuid.UUID, 0, len(rows))
	for _, row := range rows {
		if row.UploadID != nil {
			if err := i.storage.AbortLayerUpload(ctx, row.ID, *row.UploadID); err != nil {
				logger.WarnContext(ctx, "aborting a stale layer upload failed", "upload", row.ID.String(), "error", err)
				continue
			}
		}
		expired = append(expired, row.ID)
	}
	if len(expired) == 0 {
		return nil
	}
	gone, err := i.storage.DeleteLayers(ctx, expired)
	if err != nil {
		return err
	}
	if len(gone) < len(expired) {
		logger.WarnContext(ctx, "deleting layer pairs failed", "failed", len(expired)-len(gone))
	}
	if err := i.queries.DeleteUploads(ctx, gone); err != nil {
		return fmt.Errorf("delete layer upload rows: %w", err)
	}
	return nil
}

// traceparent is the trace of ctx's span, stored with a build or a
// conversion; nil when it was not sampled.
func traceparent(ctx context.Context) *string {
	if tp := telemetry.TraceParentOf(ctx); tp != "" {
		return &tp
	}
	return nil
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// traceBuild records a finished build as a span from its creation in the
// trace that asked for it.
func traceBuild(ctx context.Context, build LockBuildRow, failure string) {
	if build.Traceparent == nil {
		return
	}
	_, span := telemetry.StartIn(ctx, telemetry.TracerOf(ctx), *build.Traceparent, "images.build",
		trace.WithTimestamp(build.CreatedAt), trace.WithAttributes(attribute.String(telemetry.AttrBuild, build.ID.String()), attribute.Bool("lazycloud.mirror", build.Mirror)))
	if failure != "" {
		span.SetStatus(codes.Error, telemetry.Redact(failure))
	}
	span.End()
}
