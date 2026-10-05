package imagefs

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/klauspost/compress/zstd"
)

// maxPackedFrame bounds a compressed frame: zstd stores an incompressible
// frame raw with a few bytes per 128 KiB block.
const maxPackedFrame = FrameSize + 64<<10

// RangeReader reads bytes [off, off+n) of a stored object.
type RangeReader interface {
	ReadRange(ctx context.Context, off, n int64) (io.ReadCloser, error)
}

// StatusError is a store's refusal of a range read. An expired presigned
// URL gets 403 from S3 and 400 from Garage.
type StatusError struct {
	StatusCode int
	Body       string // the start of the response body
}

func (e *StatusError) Error() string {
	return fmt.Sprintf("object store answered %d: %s", e.StatusCode, e.Body)
}

// HTTPObject reads an object through presigned URLs; url returns the
// current one, so a refreshed grant takes effect on the next read.
func HTTPObject(client *http.Client, url func() string) RangeReader {
	return httpObject{client: client, url: url}
}

type httpObject struct {
	client *http.Client
	url    func() string
}

func (o httpObject) ReadRange(ctx context.Context, off, n int64) (io.ReadCloser, error) {
	if off < 0 || n < 0 {
		return nil, fmt.Errorf("read object range [%d, +%d): negative", off, n)
	}
	if n == 0 {
		return io.NopCloser(strings.NewReader("")), nil
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, o.url(), nil)
	if err != nil {
		return nil, fmt.Errorf("read object range: %w", redact(err))
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", off, off+n-1))
	resp, err := o.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("read object range: %w", redact(err))
	}
	if resp.StatusCode != http.StatusPartialContent {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		_ = resp.Body.Close()
		return nil, &StatusError{StatusCode: resp.StatusCode, Body: string(body)}
	}
	var start, end int64
	if _, err := fmt.Sscanf(resp.Header.Get("Content-Range"), "bytes %d-%d/", &start, &end); err != nil ||
		start != off || end != off+n-1 {
		_ = resp.Body.Close()
		return nil, fmt.Errorf("%w: asked for bytes %d-%d, got range %q", ErrInvalidIndex, off, off+n-1, resp.Header.Get("Content-Range"))
	}
	return &exactBody{body: resp.Body, left: n}, nil
}

// FetchIndex reads and decodes a stored index through a presigned URL. Like
// HTTPObject it keeps the URL's signature out of its errors.
func FetchIndex(ctx context.Context, client *http.Client, url string) ([]byte, Index, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, Index{}, fmt.Errorf("read index: %w", redact(err))
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, Index{}, fmt.Errorf("read index: %w", redact(err))
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, Index{}, &StatusError{StatusCode: resp.StatusCode, Body: string(body)}
	}
	// A stored index is compressed, so it is no larger than its decoded
	// bound.
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxIndexSize+1))
	if err != nil {
		return nil, Index{}, fmt.Errorf("read index: %w", err)
	}
	if len(raw) > maxIndexSize {
		return nil, Index{}, fmt.Errorf("%w: stored index exceeds %d bytes", ErrInvalidIndex, maxIndexSize)
	}
	ix, err := Unmarshal(raw)
	if err != nil {
		return nil, Index{}, err
	}
	return raw, ix, nil
}

// redact drops the query, which holds a presigned URL's signature, from a
// request error.
func redact(err error) error {
	var ue *url.Error
	if errors.As(err, &ue) {
		if u, perr := url.Parse(ue.URL); perr == nil {
			u.RawQuery = ""
			ue.URL = u.String()
		} else {
			ue.URL = "(unparsed URL)"
		}
	}
	return err
}

// exactBody yields exactly left bytes or fails with io.ErrUnexpectedEOF.
type exactBody struct {
	body io.ReadCloser
	left int64
}

func (b *exactBody) Read(p []byte) (int, error) {
	if b.left == 0 {
		return 0, io.EOF
	}
	if int64(len(p)) > b.left {
		p = p[:b.left]
	}
	n, err := b.body.Read(p)
	b.left -= int64(n)
	if errors.Is(err, io.EOF) && b.left > 0 {
		return n, io.ErrUnexpectedEOF
	}
	if err != nil && !errors.Is(err, io.EOF) {
		return n, fmt.Errorf("read object range: %w", err)
	}
	if b.left == 0 {
		return n, io.EOF
	}
	return n, nil
}

func (b *exactBody) Close() error {
	return b.body.Close() //nolint:wrapcheck // The body's own close error.
}

// ReadFrame returns frame i's uncompressed bytes.
func (ix Index) ReadFrame(ctx context.Context, data RangeReader, i int) ([]byte, error) {
	if i < 0 || i >= len(ix.Frames) {
		return nil, fmt.Errorf("read frame %d of %d: out of range", i, len(ix.Frames))
	}
	f := ix.Frames[i]
	body, err := data.ReadRange(ctx, f.Offset, f.Size)
	if err != nil {
		return nil, fmt.Errorf("read frame %d: %w", i, err)
	}
	defer func() { _ = body.Close() }()
	packed := make([]byte, f.Size)
	if _, err := io.ReadFull(body, packed); err != nil {
		return nil, fmt.Errorf("read frame %d: %w", i, err)
	}
	dec, err := zstd.NewReader(nil, zstd.WithDecoderConcurrency(1), zstd.WithDecoderMaxMemory(FrameSize), zstd.IgnoreChecksum(false))
	if err != nil {
		return nil, fmt.Errorf("start frame decoder: %w", err)
	}
	defer dec.Close()
	want := ix.frameLen(i)
	out, err := dec.DecodeAll(packed, make([]byte, 0, want))
	if err != nil {
		return nil, fmt.Errorf("%w: frame %d: %w", ErrInvalidIndex, i, err)
	}
	if len(out) != want {
		return nil, fmt.Errorf("%w: frame %d holds %d bytes, want %d", ErrInvalidIndex, i, len(out), want)
	}
	return out, nil
}
