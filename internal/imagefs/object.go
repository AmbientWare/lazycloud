package imagefs

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"iter"
	"net/http"
	"strings"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/aws/smithy-go"
	smithyhttp "github.com/aws/smithy-go/transport/http"
	"github.com/klauspost/compress/zstd"

	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// maxPackedFrame bounds a compressed frame: zstd stores incompressible
	// input raw with a few bytes per 128 KiB block.
	maxPackedFrame = FrameSize + 64<<10
	// maxStoredIndex bounds a stored index that decodes within
	// MaxIndexSize, by the same overhead.
	maxStoredIndex = headerSize + MaxIndexSize + 64<<10
)

// StatusError is a store's answer other than success. It keeps only the S3
// error code of the body, since the rest can echo the request's signature.
// An expired presigned URL gets 403 from S3 and 400 from Garage.
type StatusError struct {
	StatusCode int
	Code       string
}

func (e *StatusError) Error() string {
	return strings.TrimSpace(fmt.Sprintf("object store answered %d %s", e.StatusCode, e.Code))
}

// Is matches errStoreRefused for a refusal that trying again does not help.
func (e *StatusError) Is(target error) bool {
	return target == errStoreRefused && e.StatusCode < 500 &&
		e.StatusCode != http.StatusRequestTimeout && e.StatusCode != http.StatusTooManyRequests
}

// statusError reads the S3 error code from resp's body.
func statusError(resp *http.Response) *StatusError {
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024)) // a body cut short only loses the code
	_, code, _ := strings.Cut(string(body), "<Code>")
	code, _, ok := strings.Cut(code, "</Code>")
	if !ok || len(code) > 64 || strings.IndexFunc(code, func(r rune) bool {
		return (r < 'A' || r > 'Z') && (r < 'a' || r > 'z') && (r < '0' || r > '9')
	}) >= 0 {
		code = ""
	}
	return &StatusError{StatusCode: resp.StatusCode, Code: code}
}

// RangeReader reads one stored object by range through presigned URLs. url
// returns the current one, so a refreshed grant takes effect on the next
// read.
type RangeReader struct {
	client *http.Client
	url    func() string
}

// HTTPObject is the RangeReader of the object url names.
func HTTPObject(client *http.Client, url func() string) RangeReader {
	return RangeReader{client: client, url: url}
}

// read fills p with the object's bytes from off.
func (o RangeReader) read(ctx context.Context, off int64, p []byte) error {
	last := off + int64(len(p)) - 1
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, o.url(), nil)
	if err != nil {
		return fmt.Errorf("read object range: %w", telemetry.RedactURL(err))
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", off, last))
	resp, err := o.client.Do(req)
	if err != nil {
		return fmt.Errorf("read object range: %w", telemetry.RedactURL(err))
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusPartialContent {
		return statusError(resp)
	}
	var start, end int64
	if _, err := fmt.Sscanf(resp.Header.Get("Content-Range"), "bytes %d-%d/", &start, &end); err != nil || start != off || end != last {
		return fmt.Errorf("%w: asked for bytes %d-%d, got range %q", ErrInvalidIndex, off, last, resp.Header.Get("Content-Range"))
	}
	if _, err := io.ReadFull(resp.Body, p); err != nil {
		return fmt.Errorf("read object range: %w", err)
	}
	return nil
}

// Bucket reads and writes the objects of one S3 bucket, such as a
// workspace's disks.
type Bucket struct {
	client *s3.Client
	name   string
}

// NewBucket is the bucket name at endpoint in region, signed with creds.
// Checksums are computed only where S3 requires them: S3-compatible stores
// reject the streaming trailers the SDK otherwise adds to every upload.
func NewBucket(endpoint, region, name string, pathStyle bool, creds aws.CredentialsProvider, opts ...func(*s3.Options)) Bucket {
	return Bucket{name: name, client: s3.New(s3.Options{
		Region: region, BaseEndpoint: aws.String(endpoint), UsePathStyle: pathStyle, Credentials: creds,
		RequestChecksumCalculation: aws.RequestChecksumCalculationWhenRequired,
		ResponseChecksumValidation: aws.ResponseChecksumValidationWhenRequired,
	}, opts...)}
}

// Get reads the object at key, at most limit bytes. A missing object is
// fs.ErrNotExist, and a larger one ErrInvalidIndex.
func (b Bucket) Get(ctx context.Context, key string, limit int64) ([]byte, error) {
	out, err := b.client.GetObject(ctx, &s3.GetObjectInput{Bucket: &b.name, Key: &key})
	var missing *types.NoSuchKey
	if errors.As(err, &missing) {
		return nil, fmt.Errorf("s3://%s/%s: %w", b.name, key, fs.ErrNotExist)
	}
	if err != nil {
		return nil, fmt.Errorf("get s3://%s/%s: %w", b.name, key, err)
	}
	defer func() { _ = out.Body.Close() }()
	data, err := io.ReadAll(io.LimitReader(out.Body, limit+1))
	if err != nil {
		return nil, fmt.Errorf("read s3://%s/%s: %w", b.name, key, err)
	}
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("%w: s3://%s/%s is larger than %d bytes", ErrInvalidIndex, b.name, key, limit)
	}
	return data, nil
}

// GetRange fills p with the bytes of the object at key from off, while its
// ETag is etag. The store's refusal, as 412 once the object changed, is a
// *StatusError.
func (b Bucket) GetRange(ctx context.Context, key, etag string, off int64, p []byte) error {
	out, err := b.client.GetObject(ctx, &s3.GetObjectInput{
		Bucket: &b.name, Key: &key, IfMatch: &etag, Range: aws.String(fmt.Sprintf("bytes=%d-%d", off, off+int64(len(p))-1)),
	})
	var refused *smithyhttp.ResponseError
	if errors.As(err, &refused) {
		status := &StatusError{StatusCode: refused.HTTPStatusCode()}
		var api smithy.APIError
		if errors.As(err, &api) {
			status.Code = api.ErrorCode()
		}
		return status
	}
	if err != nil {
		return fmt.Errorf("get s3://%s/%s: %w", b.name, key, err)
	}
	defer func() { _ = out.Body.Close() }()
	if aws.ToInt64(out.ContentLength) != int64(len(p)) {
		return fmt.Errorf("s3://%s/%s answered %d bytes for a range of %d", b.name, key, aws.ToInt64(out.ContentLength), len(p))
	}
	if _, err := io.ReadFull(out.Body, p); err != nil {
		return fmt.Errorf("read s3://%s/%s: %w", b.name, key, err)
	}
	return nil
}

// StoredObject is one object a listing names.
type StoredObject struct {
	Key, ETag string
	Size      int64
}

// List yields the objects under prefix in key order, and an error that ends
// the listing.
func (b Bucket) List(ctx context.Context, prefix string) iter.Seq2[StoredObject, error] {
	return func(yield func(StoredObject, error) bool) {
		pages := s3.NewListObjectsV2Paginator(b.client, &s3.ListObjectsV2Input{Bucket: &b.name, Prefix: &prefix})
		for pages.HasMorePages() {
			page, err := pages.NextPage(ctx)
			if err != nil {
				yield(StoredObject{}, fmt.Errorf("list s3://%s/%s: %w", b.name, prefix, err))
				return
			}
			for _, o := range page.Contents {
				if !yield(StoredObject{Key: aws.ToString(o.Key), ETag: aws.ToString(o.ETag), Size: aws.ToInt64(o.Size)}, nil) {
					return
				}
			}
		}
	}
}

// Put writes body to key.
func (b Bucket) Put(ctx context.Context, key string, body []byte) error {
	_, err := b.client.PutObject(ctx, &s3.PutObjectInput{
		Bucket: &b.name, Key: &key, Body: bytes.NewReader(body), ContentLength: aws.Int64(int64(len(body))),
		ContentType: aws.String("application/octet-stream"),
	})
	if err != nil {
		return fmt.Errorf("put s3://%s/%s: %w", b.name, key, err)
	}
	return nil
}

// FrameReader reads frames through one decoder and keeps its buffers
// between reads. It reads up to its concurrency frames at once. Its decoder
// only decodes whole buffers, which starts no goroutines, so it is never
// closed: reads may run until the process ends.
type FrameReader struct {
	dec *zstd.Decoder
	// packed holds a buffer for each read at once, made on first use.
	packed chan []byte
}

func NewFrameReader(concurrency int) (*FrameReader, error) {
	dec, err := zstd.NewReader(nil, zstd.WithDecoderConcurrency(concurrency), zstd.WithDecoderMaxMemory(FrameSize), zstd.IgnoreChecksum(false))
	if err != nil {
		return nil, fmt.Errorf("start frame decoder: %w", err)
	}
	r := &FrameReader{dec: dec, packed: make(chan []byte, concurrency)}
	for range concurrency {
		r.packed <- nil
	}
	return r, nil
}

// Read returns frame i of ix's uncompressed bytes, read from data.
func (r *FrameReader) Read(ctx context.Context, ix Index, data RangeReader, i int) ([]byte, error) {
	if i < 0 || i >= len(ix.Frames) {
		return nil, fmt.Errorf("read frame %d of %d: out of range", i, len(ix.Frames))
	}
	f := ix.Frames[i]
	if f.Size <= 0 || f.Size > maxPackedFrame {
		return nil, fmt.Errorf("%w: frame %d of %d bytes", ErrInvalidIndex, i, f.Size)
	}
	var packed []byte
	select {
	case packed = <-r.packed:
	case <-ctx.Done():
		return nil, fmt.Errorf("read frame %d: %w", i, ctx.Err())
	}
	if packed == nil {
		packed = make([]byte, maxPackedFrame)
	}
	defer func() { r.packed <- packed }()
	if err := data.read(ctx, f.Offset, packed[:f.Size]); err != nil {
		return nil, fmt.Errorf("read frame %d: %w", i, err)
	}
	want := ix.FrameLen(i)
	out, err := r.dec.DecodeAll(packed[:f.Size], make([]byte, 0, want))
	if err != nil {
		return nil, fmt.Errorf("%w: frame %d: %w", ErrInvalidIndex, i, err)
	}
	if len(out) != want {
		return nil, fmt.Errorf("%w: frame %d holds %d bytes, want %d", ErrInvalidIndex, i, len(out), want)
	}
	return out, nil
}

// FetchIndex reads and decodes a stored index through a presigned URL,
// keeping the URL's signature out of its errors.
func FetchIndex(ctx context.Context, client *http.Client, url string) ([]byte, Index, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, Index{}, fmt.Errorf("read index: %w", telemetry.RedactURL(err))
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, Index{}, fmt.Errorf("read index: %w", telemetry.RedactURL(err))
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, Index{}, statusError(resp)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, int64(maxStoredIndex)+1))
	if err != nil {
		return nil, Index{}, fmt.Errorf("read index: %w", err)
	}
	if len(raw) > maxStoredIndex {
		return nil, Index{}, fmt.Errorf("%w: stored index exceeds %d bytes", ErrInvalidIndex, maxStoredIndex)
	}
	ix, err := Unmarshal(raw)
	if err != nil {
		return nil, Index{}, err
	}
	return raw, ix, nil
}
