package imagefs

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"golang.org/x/sync/errgroup"
)

// errStoreRefused marks a request the store refused for itself, such as a
// signature that does not match; trying again does not help.
var errStoreRefused = errors.New("the store refused the request")

// uploadParts bounds the data parts one Upload PUTs at once. Parts stream
// from the data file, so each holds no more than a request's buffers.
const uploadParts = 4

// Upload PUTs f's data in parts of partBytes to partURLs, up to uploadParts
// at once, then its index to indexURL, and returns the parts' ETags. The
// index goes last: its presence marks a complete pair. Each PUT runs under
// retry; the first part that fails ends the others.
func (f *ConvertedFile) Upload(ctx context.Context, client *http.Client, partURLs []string, partBytes int64, indexURL string,
	retry func(context.Context, func() error) error,
) ([]string, error) {
	if partBytes <= 0 || int64(len(partURLs)) != ceilDiv(f.DataBytes, partBytes) {
		return nil, fmt.Errorf("%d parts of %d bytes cannot hold %d bytes", len(partURLs), partBytes, f.DataBytes)
	}
	data, err := os.Open(f.Path)
	if err != nil {
		return nil, fmt.Errorf("open layer data file: %w", err)
	}
	defer func() { _ = data.Close() }()
	etags := make([]string, len(partURLs))
	g, partsCtx := errgroup.WithContext(ctx)
	g.SetLimit(uploadParts)
	for n, part := range partURLs {
		offset := int64(n) * partBytes
		length := min(partBytes, f.DataBytes-offset)
		g.Go(func() error {
			err := retry(partsCtx, func() error {
				var err error
				etags[n], err = putObject(partsCtx, client, part, io.NewSectionReader(data, offset, length), length)
				return err
			})
			if err != nil {
				return fmt.Errorf("upload data part %d: %w", n+1, err)
			}
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err //nolint:wrapcheck // Each part's error names it.
	}
	err = retry(ctx, func() error {
		_, err := putObject(ctx, client, indexURL, bytes.NewReader(f.Index), int64(len(f.Index)))
		return err
	})
	if err != nil {
		return nil, fmt.Errorf("upload index: %w", err)
	}
	return etags, nil
}

// putObject PUTs size bytes of body to a presigned URL and returns the
// object's ETag, keeping the URL's signature out of its errors.
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
		return "", statusError(resp)
	}
	return resp.Header.Get("ETag"), nil
}

// Retry runs fn until it succeeds, fails with a refusal of the store or an
// error final reports as final, has run attempts times, or ctx ends. It
// waits backoff after the first failure and twice as long after each next
// one.
func Retry(ctx context.Context, attempts int, backoff time.Duration, final func(error) bool, fn func() error) error {
	for attempt := 1; ; attempt++ {
		err := fn()
		if err == nil || errors.Is(err, errStoreRefused) || final(err) || attempt >= attempts || ctx.Err() != nil {
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
