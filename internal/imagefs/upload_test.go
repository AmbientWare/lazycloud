package imagefs

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// A pair's data parts go up UploadParts at once, never more, and the index
// only after every part is stored.
func TestUploadPairPutsPartsInParallelAndTheIndexLast(t *testing.T) {
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
		if inFlight == UploadParts {
			select {
			case <-full:
			default:
				close(full)
			}
		}
		mu.Unlock()
		// Parts wait until UploadParts are in flight, which only parallel
		// PUTs reach.
		if r.URL.Path != "/index" {
			select {
			case <-full:
			case <-time.After(10 * time.Second):
				t.Error("parts never reached UploadParts in flight")
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
	once := func(_ context.Context, fn func() error) error { return fn() }
	etags, err := UploadPair(t.Context(), server.Client(), bytes.NewReader(data), int64(len(data)), []byte("index"), urls, partBytes, server.URL+"/index", once)
	if err != nil {
		t.Fatal(err)
	}
	if most != UploadParts {
		t.Fatalf("%d parts were in flight at most, want %d", most, UploadParts)
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

// A part the store refuses fails the pair, and the index never goes up.
func TestUploadPairStopsAtARefusedPart(t *testing.T) {
	var mu sync.Mutex
	var paths []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		mu.Lock()
		paths = append(paths, r.URL.Path)
		mu.Unlock()
		if r.URL.Path == "/part/2" {
			http.Error(w, "SignatureDoesNotMatch", http.StatusForbidden)
			return
		}
		w.Header().Set("ETag", `"x"`)
	}))
	t.Cleanup(server.Close)
	urls := make([]string, 6)
	for n := range urls {
		urls[n] = server.URL + "/part/" + strconv.Itoa(n) + "?X-Amz-Signature=secret"
	}
	once := func(_ context.Context, fn func() error) error { return fn() }
	_, err := UploadPair(t.Context(), server.Client(), bytes.NewReader(make([]byte, 600)), 600, []byte("index"), urls, 100, server.URL+"/index", once)
	if !errors.Is(err, ErrStoreRefused) || !strings.Contains(err.Error(), "part 3") || strings.Contains(err.Error(), "secret") {
		t.Fatalf("a refused part gave %v, want the store's refusal naming part 3 without the signature", err)
	}
	mu.Lock()
	defer mu.Unlock()
	for _, p := range paths {
		if p == "/index" {
			t.Fatal("the index went up after a part failed")
		}
	}
}
