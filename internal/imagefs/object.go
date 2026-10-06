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
		return fmt.Errorf("read object range: %w", redact(err))
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", off, last))
	resp, err := o.client.Do(req)
	if err != nil {
		return fmt.Errorf("read object range: %w", redact(err))
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

// ReadFrame returns frame i's uncompressed bytes.
func (ix Index) ReadFrame(ctx context.Context, data RangeReader, i int) ([]byte, error) {
	if i < 0 || i >= len(ix.Frames) {
		return nil, fmt.Errorf("read frame %d of %d: out of range", i, len(ix.Frames))
	}
	f := ix.Frames[i]
	if f.Size <= 0 || f.Size > maxPackedFrame {
		return nil, fmt.Errorf("%w: frame %d of %d bytes", ErrInvalidIndex, i, f.Size)
	}
	packed := make([]byte, f.Size)
	if err := data.read(ctx, f.Offset, packed); err != nil {
		return nil, fmt.Errorf("read frame %d: %w", i, err)
	}
	dec, err := zstd.NewReader(nil, zstd.WithDecoderConcurrency(1), zstd.WithDecoderMaxMemory(FrameSize), zstd.IgnoreChecksum(false))
	if err != nil {
		return nil, fmt.Errorf("start frame decoder: %w", err)
	}
	defer dec.Close()
	want := ix.FrameLen(i)
	out, err := dec.DecodeAll(packed, make([]byte, 0, want))
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
		return nil, Index{}, fmt.Errorf("read index: %w", redact(err))
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, Index{}, fmt.Errorf("read index: %w", redact(err))
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
