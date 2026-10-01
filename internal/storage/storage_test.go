package storage_test

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"io"
	"net/http"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
	. "github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

func workspace(t *testing.T, pool *pgxpool.Pool) identity.WorkspaceID {
	t.Helper()
	var id uuid.UUID
	if err := pool.QueryRow(t.Context(), "insert into workspaces (name) values ('ws') returning id").Scan(&id); err != nil {
		t.Fatal(err)
	}
	dbtest.OwnWorkspaces(t, pool)
	return identity.WorkspaceID(id)
}

func put(t *testing.T, target *UploadTarget, body []byte) int {
	t.Helper()
	req, err := http.NewRequestWithContext(t.Context(), target.Method, target.URL, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	for name, value := range target.Headers {
		req.Header.Set(name, value)
	}
	req.ContentLength = int64(len(body))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("upload: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	_, _ = io.Copy(io.Discard, resp.Body)
	return resp.StatusCode
}

func TestSourceRegistration(t *testing.T) {
	ctx := t.Context()
	pool := dbtest.New(t)
	s := NewStorage(pool, storagetest.Config())
	ws := workspace(t, pool)

	archive := make([]byte, 4096)
	_, _ = rand.Read(archive)
	digest := Digest(sha256.Sum256(archive))

	first, err := s.RegisterSource(ctx, ws, digest, int64(len(archive)))
	if err != nil {
		t.Fatal(err)
	}
	if first.Present || first.Upload == nil {
		t.Fatalf("new archive: got present=%v upload=%v, want an upload target", first.Present, first.Upload)
	}

	// Bytes that do not match the digest are refused by the store.
	forged := bytes.Clone(archive)
	forged[0] ^= 0xff
	if status := put(t, first.Upload, forged); status < 400 {
		t.Fatalf("forged upload: status %d, want a rejection", status)
	}
	if again, err := s.RegisterSource(ctx, ws, digest, int64(len(archive))); err != nil || again.Present {
		t.Fatalf("after forged upload: present=%v err=%v, want missing", again.Present, err)
	}

	if status := put(t, first.Upload, archive); status != http.StatusOK {
		t.Fatalf("upload: status %d", status)
	}
	// The wrong size is not the registered archive.
	if wrong, err := s.RegisterSource(ctx, ws, digest, int64(len(archive))+1); err != nil || wrong.Present {
		t.Fatalf("wrong size: present=%v err=%v, want missing", wrong.Present, err)
	}
	registered, err := s.RegisterSource(ctx, ws, digest, int64(len(archive)))
	if err != nil || !registered.Present {
		t.Fatalf("after upload: present=%v err=%v, want present", registered.Present, err)
	}

	url, _, err := s.SourceURL(ctx, ws, digest)
	if err != nil {
		t.Fatal(err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	got, err := io.ReadAll(resp.Body)
	if err != nil || !bytes.Equal(got, archive) {
		t.Fatalf("download: status %d, %d bytes, err %v", resp.StatusCode, len(got), err)
	}
}
