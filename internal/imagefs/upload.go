package imagefs

import (
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

// PutObject PUTs size bytes of body to a presigned URL and returns the
// object's ETag. Like HTTPObject it keeps the URL's signature out of its
// errors.
func PutObject(ctx context.Context, client *http.Client, url string, body io.Reader, size int64) (string, error) {
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
