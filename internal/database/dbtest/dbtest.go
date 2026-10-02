// Package dbtest gives owner tests their own migrated PostgreSQL database on
// the compose.test.yaml server.
package dbtest

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net/url"
	"os"
	"sync"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database"
)

// defaultURL is the disposable compose.test.yaml server.
const defaultURL = "postgres://lazycloud:lazycloud@127.0.0.1:15442/postgres?sslmode=disable" //nolint:gosec // Development credentials.

// template is migrated once per test binary and cloned for each test.
type template struct {
	once sync.Once
	name string
	err  error
}

var shared template //nolint:gochecknoglobals // One migrated template per test binary.

// New creates an empty migrated database, returns a pool on it and drops it
// when the test ends. Set LAZYCLOUD_TEST_DATABASE_URL to use another server.
func New(t testing.TB) *pgxpool.Pool {
	t.Helper()
	ctx := t.Context()
	base := os.Getenv("LAZYCLOUD_TEST_DATABASE_URL")
	if base == "" {
		base = defaultURL
	}
	admin, err := pgx.Connect(ctx, base)
	if err != nil {
		t.Fatalf("connect to test postgres (docker compose -f compose.test.yaml up -d --wait): %v", err)
	}
	defer func() { _ = admin.Close(context.WithoutCancel(ctx)) }()

	shared.once.Do(func() { shared.name, shared.err = migrateTemplate(ctx, admin, base) })
	if shared.err != nil {
		t.Fatalf("migrate template database: %v", shared.err)
	}

	name := "test_" + randomSuffix()
	if _, err := admin.Exec(ctx, fmt.Sprintf("create database %s template %s", name, shared.name)); err != nil {
		t.Fatalf("create test database: %v", err)
	}
	pool, err := database.Open(ctx, withDatabase(base, name))
	if err != nil {
		t.Fatalf("open test database: %v", err)
	}
	t.Cleanup(func() {
		pool.Close()
		ctx := context.Background()
		conn, err := pgx.Connect(ctx, base)
		if err != nil {
			t.Errorf("connect to drop test database: %v", err)
			return
		}
		defer func() { _ = conn.Close(ctx) }()
		if _, err := conn.Exec(ctx, fmt.Sprintf("drop database if exists %s with (force)", name)); err != nil {
			t.Errorf("drop test database: %v", err)
		}
	})
	return pool
}

func migrateTemplate(ctx context.Context, admin *pgx.Conn, base string) (string, error) {
	name := "template_" + randomSuffix()
	if _, err := admin.Exec(ctx, "create database "+name); err != nil {
		return "", fmt.Errorf("create template: %w", err)
	}
	pool, err := database.Open(ctx, withDatabase(base, name))
	if err != nil {
		return "", err
	}
	defer pool.Close()
	if err := database.Migrate(ctx, pool); err != nil {
		return "", err
	}
	return name, nil
}

func withDatabase(base, name string) string {
	u, err := url.Parse(base)
	if err != nil {
		return base
	}
	u.Path = "/" + name
	return u.String()
}

func randomSuffix() string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}
