package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestContentCacheUploadAllowsDurableCompletion(t *testing.T) {
	payload := []byte("image layer")
	digest := sha256.Sum256(payload)
	hash := hex.EncodeToString(digest[:])
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil || !bytes.Equal(body, payload) {
			t.Error("upload body did not match")
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		// Finalizing an uploaded layer can outlast a cache read's two-second budget.
		time.Sleep(2200 * time.Millisecond)
		w.WriteHeader(http.StatusCreated)
		fmt.Fprintf(w, `{"status":"stored","content_hash":%q,"size_bytes":%d}`, hash, len(body))
	}))
	defer server.Close()
	cache, err := newContentCache(cacheConnection{Endpoint: server.URL, Token: "test-token"})
	if err != nil {
		t.Fatal(err)
	}
	stored, err := cache.store(bytes.NewReader(payload), hash, int64(len(payload)))
	if err != nil || stored != hash {
		t.Fatalf("upload failed: hash=%q error=%v", stored, err)
	}
}
