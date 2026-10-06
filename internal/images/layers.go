package images

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/storage"
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
	layerGrace = 24 * time.Hour
	// maxStoredIndex bounds the index the server reads to check an upload;
	// imagefs decodes no larger index.
	maxStoredIndex  = 64 << 20
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

// LayerReadURLs presigns GET URLs, valid for up to ttl, for every layer of
// reference, the image by digest a release pinned, in layer order, for
// host: from its region's copy of the layer bucket where that copy is
// confirmed to hold the layer, and from the layer bucket otherwise. A
// reference is converted as a whole or not at all, so an image with an
// unconverted layer is ErrNotConverted.
//
// Layers belong to the reference rather than the image id: a workspace's
// rebuild gives an image a new reference while releases that pinned the old
// one keep running it.
func (i *Images) LayerReadURLs(ctx context.Context, reference string, host compute.HostID, ttl time.Duration) (LayerReads, error) {
	rows, err := i.queries.LayerReadsFor(ctx, LayerReadsForParams{Reference: reference, Host: uuid.UUID(host)})
	if err != nil {
		return LayerReads{}, fmt.Errorf("read image layers: %w", err)
	}
	if len(rows) == 0 {
		return LayerReads{}, fmt.Errorf("%s: %w", reference, ErrNotConverted)
	}
	out := LayerReads{Layers: make([]LayerURLs, len(rows))}
	for n, row := range rows {
		region := ""
		if i.storage.HasLayerReplica(row.Region) {
			if row.Replicated {
				region = row.Region
			} else {
				out.Unconfirmed = row.Region
			}
		}
		index, expires, err := i.storage.LayerReadURL(ctx, row.ID, storage.LayerIndex, region, ttl)
		if err != nil {
			return LayerReads{}, err
		}
		data, dataExpires, err := i.storage.LayerReadURL(ctx, row.ID, storage.LayerData, region, ttl)
		if err != nil {
			return LayerReads{}, err
		}
		if dataExpires.Before(expires) {
			expires = dataExpires
		}
		out.Layers[n] = LayerURLs{DiffID: imagefs.Digest(row.DiffID), Index: index, Data: data, ExpiresAt: expires}
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
	references = slices.Clone(references)
	slices.Sort(references)
	if err := i.queries.RecordUses(ctx, slices.Compact(references)); err != nil {
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

// publication is a pushed image a build reported.
type publication struct {
	reference string
	target    buildTarget
	workspace uuid.UUID
	container uuid.UUID
	deadline  time.Time
	layers    []registryLayer
}

// conversion is a pair a host reported uploaded, as the store holds it.
type conversion struct {
	upload     uuid.UUID
	blob       string
	diffID     string
	indexBytes int64
	dataBytes  int64
	entries    int
	frames     int
}

// offer is an upload a completion hands its host. uploadID is set once the
// data upload started, for dataBytes and indexBytes.
type offer struct {
	upload                uuid.UUID
	blob                  string
	diffID                string
	uploadID              *string
	dataBytes, indexBytes int64
	// previous is the data upload sized differently that this one replaces.
	previous *string
	start    bool
}

// checkConversions completes the data uploads of the pairs the host
// reported uploaded for pushed and reads what the store holds. A pair not
// stored yet is left out and offered again. A pair that does not hold its
// layer is a failure of the build, returned as its reason.
func (i *Images) checkConversions(ctx context.Context, pushed publication, reported []UploadedLayer) ([]conversion, string, error) {
	if len(reported) == 0 {
		return nil, "", nil
	}
	diffIDs := map[string]string{}
	for _, l := range pushed.layers {
		diffIDs[l.blob] = l.diffID
	}
	etags := map[string][]string{}
	blobs := make([]string, 0, len(reported))
	for _, r := range reported {
		if _, ok := diffIDs[r.Blob]; ok {
			etags[r.Blob] = r.ETags
			blobs = append(blobs, r.Blob)
		}
	}
	uploads, err := i.queries.UploadsOf(ctx, UploadsOfParams{ContainerID: &pushed.container, Blobs: blobs})
	if err != nil {
		return nil, "", fmt.Errorf("read layer uploads: %w", err)
	}
	var out []conversion
	for _, u := range uploads {
		if u.UploadID == nil {
			continue
		}
		dataBytes, indexBytes := *u.DataBytes, *u.IndexBytes
		if parts := storage.LayerDataParts(dataBytes); int64(len(etags[u.BlobDigest])) != parts {
			return nil, fmt.Sprintf("layer %s was uploaded in %d parts, not %d", u.BlobDigest, len(etags[u.BlobDigest]), parts), nil
		}
		err := i.storage.CompleteLayerUpload(ctx, u.ID, *u.UploadID, etags[u.BlobDigest])
		var refused *storage.InvalidError
		switch {
		case errors.As(err, &refused):
			return nil, fmt.Sprintf("layer %s: %s", u.BlobDigest, refused.Reason), nil
		case err != nil && !errors.Is(err, storage.ErrNotFound):
			return nil, "", err
		}
		// A completed upload is gone, so a repeated report finds the object.
		size, err := i.storage.LayerSize(ctx, u.ID, storage.LayerData)
		if errors.Is(err, storage.ErrNotFound) {
			continue
		}
		if err != nil {
			return nil, "", err
		}
		raw, err := i.storage.ReadLayer(ctx, u.ID, storage.LayerIndex, indexBytes)
		if errors.Is(err, storage.ErrNotFound) {
			continue
		}
		if err != nil {
			return nil, "", err
		}
		ix, err := imagefs.Unmarshal(raw)
		if err != nil {
			return nil, fmt.Sprintf("the converted index of layer %s is unreadable: %v", u.BlobDigest, err), nil
		}
		if want := diffIDs[u.BlobDigest]; string(ix.Layer) != want {
			return nil, fmt.Sprintf("layer %s holds %s, but the image config names %s", u.BlobDigest, ix.Layer, want), nil
		}
		if size != dataBytes || size != ix.DataSize {
			return nil, fmt.Sprintf("the converted data of layer %s has %d bytes, its index %d", u.BlobDigest, size, ix.DataSize), nil
		}
		out = append(out, conversion{
			upload: u.ID, blob: u.BlobDigest, diffID: string(ix.Layer), indexBytes: int64(len(raw)), dataBytes: size,
			entries: len(ix.Entries), frames: len(ix.Frames),
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
// the uploads its host makes next. A config whose diff_id differs from what
// a blob holds fails the build with the returned reason.
//
// A scoped build's pairs serve only its workspace: its content is the
// workspace's, and a customer's host could upload anything. Other builds run
// on platform hosts, whose pairs serve everyone, since the blob they were
// read from is the registry's.
func (i *Images) recordLayers(ctx context.Context, q *Queries, pushed publication, converted []conversion, sizes []ConvertedLayer) ([]offer, string, error) {
	var scope *uuid.UUID
	if pushed.target.scoped {
		scope = &pushed.workspace
	}
	owner, reader := scope, scope
	// Inserts first and in blob order, so concurrent completions wait on
	// each other's pairs in one order.
	slices.SortFunc(converted, func(a, b conversion) int { return cmp.Compare(a.blob, b.blob) })
	for _, c := range converted {
		n, err := q.RecordLayer(ctx, RecordLayerParams{
			ID: c.upload, BlobDigest: c.blob, DiffID: c.diffID, WorkspaceID: owner, IndexBytes: c.indexBytes, DataBytes: c.dataBytes,
			Entries: int32(c.entries), Frames: int32(c.frames), //nolint:gosec // imagefs bounds both below 2^31.
		})
		if err != nil {
			return nil, "", fmt.Errorf("record layer: %w", err)
		}
		if n > 0 {
			err = q.ClaimUpload(ctx, c.upload)
		} else {
			err = q.AbandonUpload(ctx, AbandonUploadParams{ID: c.upload, UrlSeconds: uploadURLLifetime.Seconds()})
		}
		if err != nil {
			return nil, "", fmt.Errorf("settle layer upload: %w", err)
		}
	}
	blobs := make([]string, len(pushed.layers))
	for n, l := range pushed.layers {
		blobs[n] = l.blob
	}
	rows, err := q.UsableLayers(ctx, UsableLayersParams{Blobs: blobs, WorkspaceID: reader})
	if err != nil {
		return nil, "", fmt.Errorf("read converted layers: %w", err)
	}
	usable := map[string]UsableLayersRow{}
	for _, row := range rows {
		if have, ok := usable[row.BlobDigest]; !ok || (row.Shared && !have.Shared) {
			usable[row.BlobDigest] = row
		}
	}
	ids := make([]uuid.UUID, len(pushed.layers))
	positions := make([]int32, len(pushed.layers))
	var offers []offer
	for n, l := range pushed.layers {
		row, ok := usable[l.blob]
		if !ok {
			if !slices.ContainsFunc(offers, func(o offer) bool { return o.blob == l.blob }) {
				offers = append(offers, offer{blob: l.blob, diffID: l.diffID})
			}
			continue
		}
		if row.DiffID != l.diffID {
			return nil, fmt.Sprintf("layer %d (%s) holds %s, but the image config names %s", n, l.blob, row.DiffID, l.diffID), nil
		}
		ids[n], positions[n] = row.ID, int32(n) //nolint:gosec // At most maxImageLayers.
	}
	if len(offers) > 0 {
		blobBytes := map[string]int64{}
		for _, l := range pushed.layers {
			blobBytes[l.blob] = l.size
		}
		reported := map[string]ConvertedLayer{}
		for _, c := range sizes {
			reported[c.Blob] = c
		}
		for n := range offers {
			o := &offers[n]
			row, err := q.OfferUpload(ctx, OfferUploadParams{
				ContainerID: &pushed.container, BlobDigest: o.blob, ExpiresAt: pushed.deadline.Add(uploadGrace),
			})
			if err != nil {
				return nil, "", fmt.Errorf("offer layer upload: %w", err)
			}
			o.upload, o.uploadID = row.ID, row.UploadID
			if row.UploadID != nil {
				o.dataBytes, o.indexBytes = *row.DataBytes, *row.IndexBytes
			}
			c, ok := reported[o.blob]
			if !ok || (row.UploadID != nil && c.DataBytes == o.dataBytes && c.IndexBytes == o.indexBytes) {
				continue
			}
			if c.DataBytes < 0 || c.DataBytes > maxDataBytes(blobBytes[o.blob]) || c.IndexBytes <= 0 || c.IndexBytes > maxStoredIndex {
				return nil, fmt.Sprintf("layer %s converted to %d data and %d index bytes, more than a %d byte layer needs",
					o.blob, c.DataBytes, c.IndexBytes, blobBytes[o.blob]), nil
			}
			o.previous, o.uploadID, o.dataBytes, o.indexBytes, o.start = row.UploadID, nil, c.DataBytes, c.IndexBytes, true
		}
		return offers, "", nil
	}
	if err := q.RecordReferenceLayers(ctx, RecordReferenceLayersParams{Reference: pushed.reference, Positions: positions, LayerIds: ids}); err != nil {
		return nil, "", fmt.Errorf("record image layers: %w", err)
	}
	if err := q.TouchLayers(ctx, ids); err != nil {
		return nil, "", fmt.Errorf("touch image layers: %w", err)
	}
	if err := q.RecordUses(ctx, []string{pushed.reference}); err != nil {
		return nil, "", fmt.Errorf("record image use: %w", err)
	}
	return nil, "", nil
}

// presignUploads starts the data uploads of offers whose sizes the host
// reported, replacing one started for other sizes, and signs the URLs of
// every offer with a started upload. The others go out without URLs, to be
// converted and sized first.
func (i *Images) presignUploads(ctx context.Context, offers []offer) ([]LayerUpload, error) {
	out := make([]LayerUpload, len(offers))
	for n, o := range offers {
		out[n] = LayerUpload{Blob: o.blob, DiffID: o.diffID}
		if o.start {
			uploadID, err := i.storage.CreateLayerUpload(ctx, o.upload, o.dataBytes)
			if err != nil {
				return nil, err
			}
			started, err := i.queries.StartUpload(ctx, StartUploadParams{
				ID: o.upload, Previous: o.previous, UploadID: uploadID, DataBytes: o.dataBytes, IndexBytes: o.indexBytes,
			})
			if err == nil && started == 0 {
				err = errors.New("another report started this layer's upload; report again")
			}
			if err != nil {
				_ = i.storage.AbortLayerUpload(ctx, o.upload, uploadID)
				return nil, fmt.Errorf("start layer upload: %w", err)
			}
			if o.previous != nil {
				if err := i.storage.AbortLayerUpload(ctx, o.upload, *o.previous); err != nil {
					return nil, err
				}
			}
			o.uploadID = &uploadID
		}
		if o.uploadID == nil {
			continue
		}
		urls, err := i.storage.PresignLayerUpload(ctx, o.upload, *o.uploadID, o.dataBytes, o.indexBytes, uploadURLLifetime)
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

// LayerSweep counts what one layer sweep did.
type LayerSweep struct {
	// Retired pairs had no live image through the grace period.
	Retired int
	// Deleted pairs left the store: retired ones, lost races and abandoned
	// uploads.
	Deleted int
}

// SweepLayers runs one bounded pass over converted layers. It starts the
// grace period of pairs no live reference uses, retires those unused
// through it, and deletes the objects of retired pairs and expired uploads.
// Each pair commits on its own, so one failure keeps the others' progress;
// replicas may sweep at once, since every step is conditional on the row.
func (i *Images) SweepLayers(ctx context.Context, logger *slog.Logger) (LayerSweep, error) {
	var out LayerSweep
	if _, err := i.queries.PurgeUses(ctx, layerGrace.Seconds()); err != nil {
		return out, fmt.Errorf("purge image uses: %w", err)
	}
	if _, err := i.queries.MarkUnreferencedLayers(ctx, layerGrace.Seconds()); err != nil {
		return out, fmt.Errorf("mark unreferenced layers: %w", err)
	}
	grace := layerGrace.Seconds()
	ids, err := i.queries.UnreferencedLayers(ctx, UnreferencedLayersParams{GraceSeconds: grace, BatchSize: layerSweepBatch})
	if err != nil {
		return out, fmt.Errorf("read unreferenced layers: %w", err)
	}
	for _, id := range ids {
		n, err := i.queries.RetireLayer(ctx, RetireLayerParams{ID: id, GraceSeconds: grace})
		if err != nil {
			logger.WarnContext(ctx, "retiring an unreferenced layer failed", "layer", id.String(), "error", err)
			continue
		}
		out.Retired += int(n)
	}
	rows, err := i.queries.ExpiredUploads(ctx, layerSweepBatch)
	if err != nil {
		return out, fmt.Errorf("read expired layer uploads: %w", err)
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
		return out, nil
	}
	gone, err := i.storage.DeleteLayers(ctx, expired)
	if err != nil {
		return out, err
	}
	if len(gone) < len(expired) {
		logger.WarnContext(ctx, "deleting layer pairs failed", "failed", len(expired)-len(gone))
	}
	if err := i.queries.DeleteUploads(ctx, gone); err != nil {
		return out, fmt.Errorf("delete layer upload rows: %w", err)
	}
	out.Deleted = len(gone)
	return out, nil
}
