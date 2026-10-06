package storage

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// A converted image layer is the pair layers/<id>/index and layers/<id>/data
// in the layer bucket; images owns the rows that say which pairs exist.

// LayerObject is one object of a converted layer pair.
type LayerObject string

const (
	LayerIndex LayerObject = "index"
	LayerData  LayerObject = "data"
)

func layerKey(id uuid.UUID, object LayerObject) string {
	return "layers/" + id.String() + "/" + string(object)
}

// LayerPartBytes is the size of every part of a layer data upload but the
// last: 10,000 parts reach 640 GiB.
const LayerPartBytes = 64 << 20

// LayerUpload is where a host stores one converted layer pair: the index by
// one PUT, the data by the parts of a multipart upload, each URL signed for
// its exact Content-Length.
type LayerUpload struct {
	Index     string
	DataParts []string
	ExpiresAt time.Time
}

// CreateLayerUpload starts the multipart upload of pair id's data object of
// dataBytes. An empty data object takes no parts: it is stored now, and its
// upload id is "".
func (s *Storage) CreateLayerUpload(ctx context.Context, id uuid.UUID, dataBytes int64) (string, error) {
	if dataBytes == 0 {
		if _, err := s.client.PutObject(ctx, &s3.PutObjectInput{
			Bucket: aws.String(s.layers), Key: aws.String(layerKey(id, LayerData)), Body: bytes.NewReader(nil), ContentLength: aws.Int64(0),
		}); err != nil {
			return "", fmt.Errorf("store empty layer data: %w", err)
		}
		return "", nil
	}
	out, err := s.client.CreateMultipartUpload(ctx, &s3.CreateMultipartUploadInput{
		Bucket: aws.String(s.layers), Key: aws.String(layerKey(id, LayerData)),
	})
	if err != nil {
		return "", fmt.Errorf("create layer upload: %w", err)
	}
	return aws.ToString(out.UploadId), nil
}

// LayerDataParts is how many parts a data object of size bytes takes.
func LayerDataParts(size int64) int64 { return (size + LayerPartBytes - 1) / LayerPartBytes }

// PresignLayerUpload signs, for up to lifetime, the index PUT of indexBytes
// and every data part of the multipart upload uploadID for dataBytes. A
// request of any other length fails its signature.
func (s *Storage) PresignLayerUpload(ctx context.Context, id uuid.UUID, uploadID string, dataBytes, indexBytes int64, lifetime time.Duration) (LayerUpload, error) {
	parts := LayerDataParts(dataBytes)
	if parts > maxParts {
		return LayerUpload{}, invalid("a %d byte layer needs more than %d parts", dataBytes, maxParts)
	}
	lifetime, err := s.signedLifetime(ctx, lifetime)
	if err != nil {
		return LayerUpload{}, err
	}
	index, err := s.presign.PresignPutObject(ctx, &s3.PutObjectInput{
		Bucket: aws.String(s.layers), Key: aws.String(layerKey(id, LayerIndex)), ContentLength: aws.Int64(indexBytes),
	}, s3.WithPresignExpires(lifetime))
	if err != nil {
		return LayerUpload{}, fmt.Errorf("presign layer index upload: %w", err)
	}
	out := LayerUpload{Index: index.URL, DataParts: make([]string, parts), ExpiresAt: time.Now().Add(lifetime)}
	for n := range parts {
		req, err := s.presign.PresignUploadPart(ctx, &s3.UploadPartInput{
			Bucket: aws.String(s.layers), Key: aws.String(layerKey(id, LayerData)), UploadId: aws.String(uploadID),
			PartNumber:    aws.Int32(int32(n + 1)), //nolint:gosec // At most maxParts.
			ContentLength: aws.Int64(min(LayerPartBytes, dataBytes-n*LayerPartBytes)),
		}, s3.WithPresignExpires(lifetime))
		if err != nil {
			return LayerUpload{}, fmt.Errorf("presign layer part %d: %w", n+1, err)
		}
		out.DataParts[n] = req.URL
	}
	return out, nil
}

// CompleteLayerUpload completes the data upload uploadID from the ETags of
// its parts, in order. An upload already completed is ErrNotFound; parts
// the store does not hold are an InvalidError.
func (s *Storage) CompleteLayerUpload(ctx context.Context, id uuid.UUID, uploadID string, etags []string) error {
	if uploadID == "" {
		return nil
	}
	parts := make([]apitypes.CompletedPart, len(etags))
	for n, etag := range etags {
		parts[n] = apitypes.CompletedPart{Number: n + 1, Etag: etag}
	}
	return s.completeMultipart(ctx, s.layers, layerKey(id, LayerData), uploadID, parts)
}

// AbortLayerUpload drops the parts of data upload uploadID. An upload
// already gone counts as aborted.
func (s *Storage) AbortLayerUpload(ctx context.Context, id uuid.UUID, uploadID string) error {
	if uploadID == "" {
		return nil
	}
	return s.abortMultipart(ctx, s.layers, layerKey(id, LayerData), uploadID)
}

// LayerReadURL is a presigned GET of one object of layer pair id, valid for
// up to lifetime. Range requests read parts of it.
func (s *Storage) LayerReadURL(ctx context.Context, id uuid.UUID, object LayerObject, lifetime time.Duration) (string, time.Time, error) {
	lifetime, err := s.signedLifetime(ctx, min(lifetime, maxPresignLifetime))
	if err != nil {
		return "", time.Time{}, err
	}
	req, err := s.presign.PresignGetObject(ctx, &s3.GetObjectInput{
		Bucket: aws.String(s.layers), Key: aws.String(layerKey(id, object)),
	}, s3.WithPresignExpires(lifetime))
	if err != nil {
		return "", time.Time{}, fmt.Errorf("presign layer read: %w", err)
	}
	return req.URL, time.Now().Add(lifetime), nil
}

// LayerSize is the size of one object of layer pair id, or ErrNotFound.
func (s *Storage) LayerSize(ctx context.Context, id uuid.UUID, object LayerObject) (int64, error) {
	info, err := s.head(ctx, s.layers, layerKey(id, object))
	if err != nil {
		return 0, err
	}
	return info.Size, nil
}

// ReadLayer returns one object of layer pair id, or ErrNotFound. An object
// larger than limit bytes is an error.
func (s *Storage) ReadLayer(ctx context.Context, id uuid.UUID, object LayerObject, limit int64) ([]byte, error) {
	out, err := s.client.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(s.layers), Key: aws.String(layerKey(id, object))})
	if isNotFound(err) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("read layer %s: %w", object, err)
	}
	defer func() { _ = out.Body.Close() }()
	b, err := io.ReadAll(io.LimitReader(out.Body, limit+1))
	if err != nil {
		return nil, fmt.Errorf("read layer %s: %w", object, err)
	}
	if int64(len(b)) > limit {
		return nil, fmt.Errorf("layer %s %s is larger than %d bytes", id, object, limit)
	}
	return b, nil
}

// DeleteLayers deletes the pairs ids and returns those it removed, so a
// refusal of one keeps the others' progress. A pair already gone counts as
// removed.
func (s *Storage) DeleteLayers(ctx context.Context, ids []uuid.UUID) ([]uuid.UUID, error) {
	keys := make([]string, 0, 2*len(ids))
	for _, id := range ids {
		keys = append(keys, layerKey(id, LayerIndex), layerKey(id, LayerData))
	}
	failed, err := s.tryDeleteKeys(ctx, s.layers, keys)
	if err != nil {
		return nil, err
	}
	gone := make([]uuid.UUID, 0, len(ids))
	for _, id := range ids {
		_, index := failed[layerKey(id, LayerIndex)]
		_, data := failed[layerKey(id, LayerData)]
		if !index && !data {
			gone = append(gone, id)
		}
	}
	return gone, nil
}
