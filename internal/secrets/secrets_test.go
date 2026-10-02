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

// A secret lists the workloads whose active release receives it.
func TestSecretsShowTheWorkloadsThatReceiveThem(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	ctx := t.Context()
	s := NewSecrets(pool, newKey(t))
	ws := newWorkspace(t, pool, "acme")
	digest := make([]byte, 32)
	var app, fn, release uuid.UUID
	scan := func(into *uuid.UUID, sql string, args ...any) {
		t.Helper()
		if err := pool.QueryRow(ctx, sql, args...).Scan(into); err != nil {
			t.Fatalf("%s: %v", sql, err)
		}
	}
	scan(&app, `insert into apps (workspace_id, name, state) values ($1, 'shop', 'active') returning id`, uuid.UUID(ws))
	scan(&fn, `insert into workloads (app_id, kind, name, desired_state) values ($1, 'function', 'checkout', 'active') returning id`, app)
	scan(&release, `insert into releases (workload_id, version, spec, spec_digest, source_sha256)
		values ($1, 1, '{"secrets": ["API_TOKEN"]}', $2, $2) returning id`, fn, digest)
	scan(&fn, `update workloads set active_release_id = $1 where id = $2 returning id`, release, fn)

	created, err := s.Create(ctx, ws, "API_TOKEN", "value")
	if err != nil || len(created.UsedBy) != 1 || created.UsedBy[0] != (Use{App: "shop", Kind: "function", Workload: "checkout"}) {
		t.Fatalf("created %+v %v", created, err)
	}
	if _, err := s.Create(ctx, ws, "UNUSED", "value"); err != nil {
		t.Fatal(err)
	}
	listed, _, err := s.List(ctx, ws, "", 10)
	if err != nil || len(listed) != 2 || len(listed[0].UsedBy) != 1 || listed[1].UsedBy == nil || len(listed[1].UsedBy) != 0 {
		t.Fatalf("listed %+v %v", listed, err)
	}
	// A stopped workload no longer receives it.
	if _, err := pool.Exec(ctx, `update workloads set desired_state = 'stopped'`); err != nil {
		t.Fatal(err)
	}
	if got, err := s.Get(ctx, ws, "API_TOKEN"); err != nil || len(got.UsedBy) != 0 {
		t.Fatalf("after stop %+v %v", got, err)
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
