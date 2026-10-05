package storage_test

import (
	"bytes"
	"crypto/rand"
	"errors"
	"io"
	"net/http"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	. "github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// putPart PUTs body to a presigned URL and returns the status and ETag.
func putPart(t *testing.T, url string, body []byte) (int, string) {
	t.Helper()
	req, err := http.NewRequestWithContext(t.Context(), http.MethodPut, url, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	return resp.StatusCode, resp.Header.Get("ETag")
}

// A layer's data goes up in signed parts of LayerPartBytes and reads back
// whole; a part or index of any other length than signed is refused, and an
// aborted upload stores nothing.
func TestLayerDataUploadsInSignedParts(t *testing.T) {
	store := NewStorage(dbtest.New(t), storagetest.Config())
	ctx := t.Context()
	data := make([]byte, 2*LayerPartBytes+1234)
	if _, err := rand.Read(data); err != nil {
		t.Fatal(err)
	}
	index := []byte("index")
	id := uuid.New()
	uploadID, err := store.CreateLayerUpload(ctx, id, int64(len(data)))
	if err != nil {
		t.Fatal(err)
	}
	urls, err := store.PresignLayerUpload(ctx, id, uploadID, int64(len(data)), int64(len(index)), time.Minute)
	if err != nil || len(urls.DataParts) != 3 {
		t.Fatalf("want 3 parts, got %d %v", len(urls.DataParts), err)
	}
	if status, _ := putPart(t, urls.DataParts[2], append(bytes.Clone(data[2*LayerPartBytes:]), 0)); status != http.StatusForbidden {
		t.Fatalf("a part longer than signed answered %d", status)
	}
	if status, _ := putPart(t, urls.Index, []byte("a longer index")); status != http.StatusForbidden {
		t.Fatalf("an index longer than signed answered %d", status)
	}
	etags := make([]string, len(urls.DataParts))
	for n, part := range urls.DataParts {
		end := min(int64(n+1)*LayerPartBytes, int64(len(data)))
		status, etag := putPart(t, part, data[int64(n)*LayerPartBytes:end])
		if status != http.StatusOK {
			t.Fatalf("part %d answered %d", n+1, status)
		}
		etags[n] = etag
	}
	if status, _ := putPart(t, urls.Index, index); status != http.StatusOK {
		t.Fatalf("index answered %d", status)
	}
	if err := store.CompleteLayerUpload(ctx, id, uploadID, etags); err != nil {
		t.Fatal(err)
	}
	if err := store.CompleteLayerUpload(ctx, id, uploadID, etags); !errors.Is(err, ErrNotFound) {
		t.Fatalf("completing again: %v", err)
	}
	if size, err := store.LayerSize(ctx, id, LayerData); err != nil || size != int64(len(data)) {
		t.Fatalf("stored %d bytes, want %d: %v", size, len(data), err)
	}
	read, _, err := store.LayerReadURL(ctx, id, LayerData, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, read, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	got, err := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if err != nil || !bytes.Equal(got, data) {
		t.Fatalf("the data reads back other bytes: %v", err)
	}

	aborted := uuid.New()
	uploadID, err = store.CreateLayerUpload(ctx, aborted, 10)
	if err != nil {
		t.Fatal(err)
	}
	urls, err = store.PresignLayerUpload(ctx, aborted, uploadID, 10, 1, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if status, _ := putPart(t, urls.DataParts[0], make([]byte, 10)); status != http.StatusOK {
		t.Fatalf("part answered %d", status)
	}
	if err := store.AbortLayerUpload(ctx, aborted, uploadID); err != nil {
		t.Fatal(err)
	}
	if err := store.AbortLayerUpload(ctx, aborted, uploadID); err != nil {
		t.Fatalf("aborting again: %v", err)
	}
	if status, _ := putPart(t, urls.DataParts[0], make([]byte, 10)); status == http.StatusOK {
		t.Fatal("an aborted upload took a part")
	}
	if _, err := store.LayerSize(ctx, aborted, LayerData); !errors.Is(err, ErrNotFound) {
		t.Fatalf("an aborted upload stored data: %v", err)
	}

	// An empty data object takes no parts and is stored at once.
	empty := uuid.New()
	uploadID, err = store.CreateLayerUpload(ctx, empty, 0)
	if err != nil {
		t.Fatal(err)
	}
	urls, err = store.PresignLayerUpload(ctx, empty, uploadID, 0, 1, time.Minute)
	if err != nil || len(urls.DataParts) != 0 {
		t.Fatalf("an empty layer has %d parts: %v", len(urls.DataParts), err)
	}
	if err := store.CompleteLayerUpload(ctx, empty, uploadID, nil); err != nil {
		t.Fatal(err)
	}
	if size, err := store.LayerSize(ctx, empty, LayerData); err != nil || size != 0 {
		t.Fatalf("an empty layer stored %d bytes: %v", size, err)
	}
}
