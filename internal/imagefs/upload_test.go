package imagefs

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// convertedFile is a ConvertedFile of data and index, written under a test
// directory.
func convertedFile(t *testing.T, data []byte, index string) *ConvertedFile {
	t.Helper()
	path := filepath.Join(t.TempDir(), "data")
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	return &ConvertedFile{Path: path, DataBytes: int64(len(data)), Index: []byte(index)}
}

func once(_ context.Context, fn func() error) error { return fn() }

// A pair's data parts go up uploadParts at once, never more, and the index
// only after every part is stored.
func TestUploadPutsPartsInParallelAndTheIndexLast(t *testing.T) {
	const parts, partBytes = 10, 1000
	data := bytes.Repeat([]byte("0123456789"), parts*partBytes/10-5)
	var mu sync.Mutex
	inFlight, most, stored := 0, 0, map[string][]byte{}
	full := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		if r.URL.Path == "/index" && len(stored) != parts {
			mu.Unlock()
			t.Errorf("the index went up with %d of %d parts stored", len(stored), parts)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		inFlight++
		most = max(most, inFlight)
		if inFlight == uploadParts {
			select {
			case <-full:
			default:
				close(full)
			}
		}
		mu.Unlock()
		// Parts wait until uploadParts are in flight, which only parallel
		// PUTs reach.
		if r.URL.Path != "/index" {
			select {
			case <-full:
			case <-time.After(10 * time.Second):
				t.Error("parts never reached uploadParts in flight")
			}
		}
		mu.Lock()
		inFlight--
		stored[r.URL.Path] = body
		mu.Unlock()
		w.Header().Set("ETag", `"`+r.URL.Path+`"`)
	}))
	t.Cleanup(server.Close)
	urls := make([]string, parts)
	for n := range urls {
		urls[n] = server.URL + "/part/" + strconv.Itoa(n)
	}
	etags, err := convertedFile(t, data, "index").Upload(t.Context(), server.Client(), urls, partBytes, server.URL+"/index", once)
	if err != nil {
		t.Fatal(err)
	}
	if most != uploadParts {
		t.Fatalf("%d parts were in flight at most, want %d", most, uploadParts)
	}
	var joined []byte
	for n, etag := range etags {
		path := "/part/" + strconv.Itoa(n)
		if etag != `"`+path+`"` {
			t.Fatalf("part %d has ETag %s", n, etag)
		}
		joined = append(joined, stored[path]...)
	}
	if !bytes.Equal(joined, data) || string(stored["/index"]) != "index" {
		t.Fatal("the stored parts are not the data, or the index is missing")
	}
}

// A part the store refuses fails the pair without a retry, its error keeps
// the store's code but not the signature the store echoes, and the index
// never goes up.
func TestUploadStopsAtARefusedPart(t *testing.T) {
	var mu sync.Mutex
	var paths []string
	refused := refusingStore(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		mu.Lock()
		paths = append(paths, r.URL.Path)
		mu.Unlock()
		if r.URL.Path == "/part/2" {
			refused.Config.Handler.ServeHTTP(w, r)
			return
		}
		w.Header().Set("ETag", `"x"`)
	}))
	t.Cleanup(server.Close)
	urls := make([]string, 6)
	for n := range urls {
		urls[n] = server.URL + "/part/" + strconv.Itoa(n) + "?X-Amz-Signature=secret"
	}
	retry := func(ctx context.Context, fn func() error) error {
		return Retry(ctx, 3, time.Millisecond, func(error) bool { return false }, fn)
	}
	_, err := convertedFile(t, make([]byte, 600), "index").Upload(t.Context(), server.Client(), urls, 100, server.URL+"/index", retry)
	if !errors.Is(err, errStoreRefused) || !strings.Contains(err.Error(), "part 3") ||
		!strings.Contains(err.Error(), "SignatureDoesNotMatch") || strings.Contains(err.Error(), "secret") {
		t.Fatalf("a refused part gave %v, want the store's refusal naming part 3 without the signature", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if n := strings.Count(strings.Join(paths, " "), "/part/2"); n != 1 {
		t.Fatalf("the refused part was sent %d times", n)
	}
	for _, p := range paths {
		if p == "/index" {
			t.Fatal("the index went up after a part failed")
		}
	}
}
