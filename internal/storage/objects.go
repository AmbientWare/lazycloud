package storage

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	s3types "github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/aws/smithy-go"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

const (
	// maxParts is S3's part limit for one multipart upload.
	maxParts = 10_000
	// maxCopyBytes is the largest object one CopyObject can copy; larger
	// objects copy in parts of copyPartBytes.
	maxCopyBytes  = 5 << 30
	copyPartBytes = 1 << 30
	// maxPresignLifetime is the longest a SigV4 presigned URL can last.
	maxPresignLifetime = 7 * 24 * time.Hour
	// deleteBatch is the most keys one DeleteObjects call removes.
	deleteBatch = 1000
)

func isNotFound(err error) bool {
	var apiErr smithy.APIError
	return errors.As(err, &apiErr) && (apiErr.ErrorCode() == "NotFound" || apiErr.ErrorCode() == "NoSuchKey")
}

// presignPart returns a presigned PUT of one multipart part.
func (s *Storage) presignPart(ctx context.Context, bucket, key, uploadID string, number int32, lifetime time.Duration) (string, error) {
	req, err := s.presign.PresignUploadPart(ctx, &s3.UploadPartInput{
		Bucket: aws.String(bucket), Key: aws.String(key), UploadId: aws.String(uploadID), PartNumber: aws.Int32(number),
	}, s3.WithPresignExpires(lifetime))
	if err != nil {
		return "", fmt.Errorf("presign part %d: %w", number, err)
	}
	return req.URL, nil
}

// startMultipart creates a multipart upload of size bytes and presigns every
// part. size 0 still gets one (empty) part.
func (s *Storage) startMultipart(ctx context.Context, bucket, key, contentType string, size, partSize int64, lifetime time.Duration) (string, []apitypes.UploadPart, error) {
	count := max((size+partSize-1)/partSize, 1)
	if count > maxParts {
		return "", nil, invalid("%d bytes in parts of %d bytes needs more than %d parts", size, partSize, maxParts)
	}
	input := &s3.CreateMultipartUploadInput{Bucket: aws.String(bucket), Key: aws.String(key)}
	if contentType != "" {
		input.ContentType = aws.String(contentType)
	}
	created, err := s.client.CreateMultipartUpload(ctx, input)
	if err != nil {
		return "", nil, fmt.Errorf("create multipart upload: %w", err)
	}
	uploadID := aws.ToString(created.UploadId)
	parts := make([]apitypes.UploadPart, 0, count)
	for n := range count {
		offset := n * partSize
		url, err := s.presignPart(ctx, bucket, key, uploadID, int32(n+1), lifetime) //nolint:gosec // At most maxParts.
		if err != nil {
			return "", nil, err
		}
		parts = append(parts, apitypes.UploadPart{
			Number: int(n + 1), Offset: offset, SizeBytes: min(partSize, size-offset), Url: url,
		})
	}
	return uploadID, parts, nil
}

func (s *Storage) completeMultipart(ctx context.Context, bucket, key, uploadID string, parts []apitypes.CompletedPart) error {
	completed := make([]s3types.CompletedPart, len(parts))
	for n, p := range parts {
		completed[n] = s3types.CompletedPart{PartNumber: aws.Int32(int32(p.Number)), ETag: aws.String(p.Etag)} //nolint:gosec // The schema caps part numbers.
	}
	_, err := s.client.CompleteMultipartUpload(ctx, &s3.CompleteMultipartUploadInput{
		Bucket: aws.String(bucket), Key: aws.String(key), UploadId: aws.String(uploadID),
		MultipartUpload: &s3types.CompletedMultipartUpload{Parts: completed},
	})
	var apiErr smithy.APIError
	if errors.As(err, &apiErr) {
		switch apiErr.ErrorCode() {
		case "NoSuchUpload":
			return ErrNotFound
		case "InvalidPart", "InvalidPartOrder", "EntityTooSmall", "MalformedXML":
			return invalid("the upload cannot complete: %s", apiErr.ErrorMessage())
		}
	}
	if err != nil {
		return fmt.Errorf("complete multipart upload: %w", err)
	}
	return nil
}

func (s *Storage) abortMultipart(ctx context.Context, bucket, key, uploadID string) error {
	_, err := s.client.AbortMultipartUpload(ctx, &s3.AbortMultipartUploadInput{
		Bucket: aws.String(bucket), Key: aws.String(key), UploadId: aws.String(uploadID),
	})
	var apiErr smithy.APIError
	if errors.As(err, &apiErr) && apiErr.ErrorCode() == "NoSuchUpload" {
		return nil
	}
	if err != nil {
		return fmt.Errorf("abort multipart upload: %w", err)
	}
	return nil
}

// objectInfo is a stored object.
type objectInfo struct {
	Key      string
	Size     int64
	Modified time.Time
}

// head returns the object at key, or ErrNotFound.
func (s *Storage) head(ctx context.Context, bucket, key string) (objectInfo, error) {
	out, err := s.client.HeadObject(ctx, &s3.HeadObjectInput{Bucket: aws.String(bucket), Key: aws.String(key)})
	if isNotFound(err) {
		return objectInfo{}, ErrNotFound
	}
	if err != nil {
		return objectInfo{}, fmt.Errorf("head object: %w", err)
	}
	return objectInfo{Key: key, Size: aws.ToInt64(out.ContentLength), Modified: aws.ToTime(out.LastModified)}, nil
}

// eachObject calls fn for every object under prefix, one listing page at a
// time.
func (s *Storage) eachObject(ctx context.Context, bucket, prefix string, fn func([]objectInfo) error) error {
	pages := s3.NewListObjectsV2Paginator(s.client, &s3.ListObjectsV2Input{Bucket: aws.String(bucket), Prefix: aws.String(prefix)})
	for pages.HasMorePages() {
		page, err := pages.NextPage(ctx)
		if err != nil {
			return fmt.Errorf("list objects: %w", err)
		}
		batch := make([]objectInfo, len(page.Contents))
		for n, o := range page.Contents {
			batch[n] = objectInfo{Key: aws.ToString(o.Key), Size: aws.ToInt64(o.Size), Modified: aws.ToTime(o.LastModified)}
		}
		if err := fn(batch); err != nil {
			return err
		}
	}
	return nil
}

// deleteKeys removes keys in batches. A key that is already gone counts as
// deleted.
func (s *Storage) deleteKeys(ctx context.Context, bucket string, keys []string) error {
	failed, err := s.tryDeleteKeys(ctx, bucket, keys)
	if err != nil {
		return err
	}
	for key, reason := range failed {
		return fmt.Errorf("delete %s: %s", key, reason)
	}
	return nil
}

// tryDeleteKeys removes keys and returns those the store refused, with its
// reason, so callers keep what succeeded.
func (s *Storage) tryDeleteKeys(ctx context.Context, bucket string, keys []string) (map[string]string, error) {
	failed := map[string]string{}
	for start := 0; start < len(keys); start += deleteBatch {
		batch := keys[start:min(start+deleteBatch, len(keys))]
		objects := make([]s3types.ObjectIdentifier, len(batch))
		for n, key := range batch {
			objects[n] = s3types.ObjectIdentifier{Key: aws.String(key)}
		}
		out, err := s.client.DeleteObjects(ctx, &s3.DeleteObjectsInput{
			Bucket: aws.String(bucket), Delete: &s3types.Delete{Objects: objects, Quiet: aws.Bool(true)},
		})
		if err != nil {
			return nil, fmt.Errorf("delete objects: %w", err)
		}
		for _, e := range out.Errors {
			failed[aws.ToString(e.Key)] = aws.ToString(e.Message)
		}
	}
	return failed, nil
}

// prefixChunk is the most objects one sweep step deletes under a prefix.
const prefixChunk = 1000

// deletePrefixChunk aborts the prefix's multipart uploads and deletes up to
// prefixChunk of its objects. It reports whether the prefix is now empty.
func (s *Storage) deletePrefixChunk(ctx context.Context, bucket, prefix string) (bool, error) {
	uploads, err := s.client.ListMultipartUploads(ctx, &s3.ListMultipartUploadsInput{
		Bucket: aws.String(bucket), Prefix: aws.String(prefix), MaxUploads: aws.Int32(prefixChunk),
	})
	if err != nil {
		return false, fmt.Errorf("list multipart uploads: %w", err)
	}
	for _, u := range uploads.Uploads {
		if err := s.abortMultipart(ctx, bucket, aws.ToString(u.Key), aws.ToString(u.UploadId)); err != nil {
			return false, err
		}
	}
	out, err := s.client.ListObjectsV2(ctx, &s3.ListObjectsV2Input{
		Bucket: aws.String(bucket), Prefix: aws.String(prefix), MaxKeys: aws.Int32(prefixChunk),
	})
	if err != nil {
		return false, fmt.Errorf("list objects: %w", err)
	}
	keys := make([]string, len(out.Contents))
	for n, o := range out.Contents {
		keys[n] = aws.ToString(o.Key)
	}
	if err := s.deleteKeys(ctx, bucket, keys); err != nil {
		return false, err
	}
	return !aws.ToBool(out.IsTruncated) && !aws.ToBool(uploads.IsTruncated), nil
}

// copyObject copies one object within bucket, in parts above maxCopyBytes.
func (s *Storage) copyObject(ctx context.Context, bucket string, from objectInfo, to string) error {
	// CopySource is a URL path: each key segment is escaped, or a key
	// holding "%20" would copy another object.
	segments := strings.Split(from.Key, "/")
	for n, segment := range segments {
		segments[n] = url.PathEscape(segment)
	}
	source := bucket + "/" + strings.Join(segments, "/")
	if from.Size <= maxCopyBytes {
		if _, err := s.client.CopyObject(ctx, &s3.CopyObjectInput{
			Bucket: aws.String(bucket), Key: aws.String(to), CopySource: aws.String(source),
		}); err != nil {
			return fmt.Errorf("copy %s: %w", from.Key, err)
		}
		return nil
	}
	created, err := s.client.CreateMultipartUpload(ctx, &s3.CreateMultipartUploadInput{Bucket: aws.String(bucket), Key: aws.String(to)})
	if err != nil {
		return fmt.Errorf("start copy of %s: %w", from.Key, err)
	}
	uploadID := aws.ToString(created.UploadId)
	var parts []apitypes.CompletedPart
	for offset, n := int64(0), 1; offset < from.Size; offset, n = offset+copyPartBytes, n+1 {
		end := min(offset+copyPartBytes, from.Size) - 1
		out, err := s.client.UploadPartCopy(ctx, &s3.UploadPartCopyInput{
			Bucket: aws.String(bucket), Key: aws.String(to), UploadId: aws.String(uploadID),
			PartNumber: aws.Int32(int32(n)), CopySource: aws.String(source), //nolint:gosec // 5 TiB / 1 GiB parts.
			CopySourceRange: aws.String(fmt.Sprintf("bytes=%d-%d", offset, end)),
		})
		if err != nil {
			return errors.Join(fmt.Errorf("copy part %d of %s: %w", n, from.Key, err), s.abortMultipart(context.WithoutCancel(ctx), bucket, to, uploadID))
		}
		parts = append(parts, apitypes.CompletedPart{Number: n, Etag: aws.ToString(out.CopyPartResult.ETag)})
	}
	return s.completeMultipart(ctx, bucket, to, uploadID, parts)
}

// presignLifetime clamps a requested lifetime to what SigV4 allows.
func presignLifetime(seconds *int) time.Duration {
	lifetime := time.Hour
	if seconds != nil {
		lifetime = time.Duration(*seconds) * time.Second
	}
	return min(max(lifetime, time.Second), maxPresignLifetime)
}
