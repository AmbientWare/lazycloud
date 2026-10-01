package secrets

import (
	"bytes"
	"crypto/rand"
	"errors"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

func newKey(t *testing.T) *FileKey {
	t.Helper()
	raw := make([]byte, masterKeyBytes)
	if _, err := rand.Read(raw); err != nil {
		t.Fatal(err)
	}
	key, err := NewFileKey(raw)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func newWorkspace(t *testing.T, pool *pgxpool.Pool, name string) identity.WorkspaceID {
	t.Helper()
	var id uuid.UUID
	if err := pool.QueryRow(t.Context(), "insert into workspaces (name) values ($1) returning id", name).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return identity.WorkspaceID(id)
}

func TestSecretLifecycle(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	ctx := t.Context()
	s := NewSecrets(pool, newKey(t))
	ws := newWorkspace(t, pool, "acme")

	if _, err := s.Create(ctx, ws, "API_TOKEN", "first"); err != nil {
		t.Fatal(err)
	}
	var exists *ExistsError
	if _, err := s.Create(ctx, ws, "API_TOKEN", "again"); !errors.As(err, &exists) || err.Error() != "secret already exists: API_TOKEN" {
		t.Fatalf("duplicate create = %v", err)
	}
	if _, err := s.Update(ctx, ws, "API_TOKEN", "second"); err != nil {
		t.Fatal(err)
	}
	if _, value, err := s.Reveal(ctx, ws, "API_TOKEN"); err != nil || value != "second" {
		t.Fatalf("reveal = %q, %v", value, err)
	}
	var missing *NotFoundError
	if _, err := s.Update(ctx, ws, "OTHER", "x"); !errors.As(err, &missing) || err.Error() != "secret not found: OTHER" {
		t.Fatalf("update of a missing secret = %v", err)
	}
	if _, err := s.Set(ctx, ws, "OTHER", "upserted"); err != nil {
		t.Fatal(err)
	}
	values, err := s.Resolve(ctx, ws, []string{"API_TOKEN", "OTHER"})
	if err != nil || values["API_TOKEN"] != "second" || values["OTHER"] != "upserted" {
		t.Fatalf("resolve = %v, %v", values, err)
	}
	if _, err := s.Resolve(ctx, ws, []string{"API_TOKEN", "ABSENT"}); !errors.As(err, &missing) || missing.Name != "ABSENT" {
		t.Fatalf("resolve with a missing name = %v", err)
	}
	var reserved *ReservedNameError
	if _, err := s.Set(ctx, ws, "LAZYCLOUD_TOKEN", "x"); !errors.As(err, &reserved) {
		t.Fatalf("reserved name = %v", err)
	}
	if err := s.Delete(ctx, ws, "OTHER"); err != nil {
		t.Fatal(err)
	}
	if err := s.Delete(ctx, ws, "OTHER"); !errors.As(err, &missing) {
		t.Fatalf("second delete = %v", err)
	}

	// Another workspace sees none of it.
	other := newWorkspace(t, pool, "other")
	if _, err := s.Get(ctx, other, "API_TOKEN"); !errors.As(err, &missing) {
		t.Fatalf("cross-workspace get = %v", err)
	}
}

func TestValuesAreSealedAndBoundToTheirName(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	ctx := t.Context()
	key := newKey(t)
	s := NewSecrets(pool, key)
	ws := newWorkspace(t, pool, "acme")
	if _, err := s.Create(ctx, ws, "DB_PASSWORD", "hunter2-hunter2"); err != nil {
		t.Fatal(err)
	}
	var stored []byte
	if err := pool.QueryRow(ctx, "select wrapped_key || nonce || ciphertext from secrets").Scan(&stored); err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(stored, []byte("hunter2")) {
		t.Fatal("the stored row contains the plaintext")
	}

	// A row moved to another name no longer opens.
	if _, err := pool.Exec(ctx, "update secrets set name = 'STOLEN'"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.Reveal(ctx, ws, "STOLEN"); err == nil {
		t.Fatal("a renamed ciphertext opened")
	}
	if _, err := pool.Exec(ctx, "update secrets set name = 'DB_PASSWORD'"); err != nil {
		t.Fatal(err)
	}

	// A server holding another master key cannot read it.
	if _, _, err := NewSecrets(pool, newKey(t)).Reveal(ctx, ws, "DB_PASSWORD"); !errors.Is(err, ErrUnknownKey) {
		t.Fatalf("reveal with another master key = %v", err)
	}
}

func TestListPages(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	ctx := t.Context()
	s := NewSecrets(pool, newKey(t))
	ws := newWorkspace(t, pool, "acme")
	for _, name := range []string{"C", "A", "B", "E", "D"} {
		if _, err := s.Create(ctx, ws, name, "v"); err != nil {
			t.Fatal(err)
		}
	}
	var names []string
	cursor := ""
	for range 10 {
		page, next, err := s.List(ctx, ws, cursor, 2)
		if err != nil {
			t.Fatal(err)
		}
		for _, secret := range page {
			names = append(names, secret.Name)
		}
		if next == "" {
			break
		}
		cursor = next
	}
	if got := len(names); got != 5 || names[0] != "A" || names[4] != "E" {
		t.Fatalf("paged names = %v", names)
	}
}

func TestSigningKeyIsCreatedOnce(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	ctx := t.Context()
	s := NewSecrets(pool, newKey(t))
	ws := newWorkspace(t, pool, "acme")
	first, err := s.SigningKey(ctx, ws)
	if err != nil || first == "" {
		t.Fatalf("signing key = %q, %v", first, err)
	}
	second, err := s.SigningKey(ctx, ws)
	if err != nil || second != first {
		t.Fatalf("second signing key = %q, %v", second, err)
	}
}
