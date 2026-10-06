package imagefs

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// ErrStoreRefused marks a request the store refused for itself, such as a
// signature that does not match; trying again does not help.
var ErrStoreRefused = errors.New("the store refused the request")

// ConvertLayer converts layer, which the image config names diffID, into
// data and returns its index and the index encoded. A layer Convert refuses
// or whose digest is not diffID is ErrInvalidLayer.
func ConvertLayer(ctx context.Context, layer io.Reader, data io.Writer, diffID Digest) (Index, []byte, error) {
	ix, err := Convert(ctx, layer, data)
	if err != nil {
		return Index{}, nil, err
	}
	if ix.Layer != diffID {
		return Index{}, nil, fmt.Errorf("%w: its content is %s, but the image config names %s", ErrInvalidLayer, ix.Layer, diffID)
	}
	index, err := ix.Marshal()
	if err != nil {
		return Index{}, nil, fmt.Errorf("encode index: %w", err)
	}
	return ix, index, nil
}

// UploadPair PUTs dataBytes of data in parts of partBytes to partURLs, then
// index to indexURL, and returns the parts' ETags. The index goes last: its
// presence marks a complete pair. Each PUT runs under retry.
func UploadPair(ctx context.Context, client *http.Client, data io.ReaderAt, dataBytes int64, index []byte,
	partURLs []string, partBytes int64, indexURL string, retry func(context.Context, func() error) error,
) ([]string, error) {
	if partBytes <= 0 || int64(len(partURLs)) != (dataBytes+partBytes-1)/partBytes {
		return nil, fmt.Errorf("%d parts of %d bytes cannot hold %d bytes", len(partURLs), partBytes, dataBytes)
	}
	etags := make([]string, len(partURLs))
	for n, part := range partURLs {
		offset := int64(n) * partBytes
		length := min(partBytes, dataBytes-offset)
		err := retry(ctx, func() error {
			var err error
			etags[n], err = putObject(ctx, client, part, io.NewSectionReader(data, offset, length), length)
			return err
		})
		if err != nil {
			return nil, fmt.Errorf("upload data part %d: %w", n+1, err)
		}
	}
	err := retry(ctx, func() error {
		_, err := putObject(ctx, client, indexURL, bytes.NewReader(index), int64(len(index)))
		return err
	})
	if err != nil {
		return nil, fmt.Errorf("upload index: %w", err)
	}
	return etags, nil
}

// putObject PUTs size bytes of body to a presigned URL and returns the
// object's ETag. Like HTTPObject it keeps the URL's signature out of its
// errors.
func putObject(ctx context.Context, client *http.Client, url string, body io.Reader, size int64) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, url, body)
	if err != nil {
		return "", fmt.Errorf("build upload request: %w", redact(err))
	}
	req.ContentLength = size
	if size == 0 {
		req.Body = http.NoBody
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("upload: %w", redact(err))
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		detail, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		err := fmt.Errorf("the store answered %s: %s", resp.Status, strings.TrimSpace(string(detail)))
		if resp.StatusCode < 500 && resp.StatusCode != http.StatusTooManyRequests && resp.StatusCode != http.StatusRequestTimeout {
			err = fmt.Errorf("%w: %w", ErrStoreRefused, err)
		}
		return "", err
	}
	return resp.Header.Get("ETag"), nil
}

// Retry runs fn until it succeeds, fails with ErrStoreRefused or an error
// final reports as final, has run attempts times, or ctx ends. It waits
// backoff after the first failure and twice as long after each next one.
func Retry(ctx context.Context, attempts int, backoff time.Duration, final func(error) bool, fn func() error) error {
	for attempt := 1; ; attempt++ {
		err := fn()
		if err == nil || errors.Is(err, ErrStoreRefused) || final(err) || attempt >= attempts || ctx.Err() != nil {
			return err
		}
		timer := time.NewTimer(backoff)
		select {
		case <-ctx.Done():
			timer.Stop()
			return err
		case <-timer.C:
		}
		backoff *= 2
	}
}
