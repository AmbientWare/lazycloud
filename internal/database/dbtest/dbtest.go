// Package dbtest gives owner tests their own migrated PostgreSQL database on
// the compose.test.yaml server.
package dbtest

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/migrations"
)

// defaultURL is the disposable compose.test.yaml server.
const defaultURL = "postgres://lazycloud:lazycloud@127.0.0.1:15442/postgres?sslmode=disable" //nolint:gosec // Development credentials.

// template is a migrated database each test of a binary clones.
type template struct {
	once sync.Once
	name string
	err  error
}

var shared template //nolint:gochecknoglobals // The migration chain's template, found once per test binary.

// staleAfter is how long a database outlives its last use. A binary drops
// its clones as its tests end, and a killed binary leaves them, so the first
// connection of each binary drops what earlier ones left: clones and
// prepared templates made more than staleAfter ago, and chain templates no
// binary has used for as long.
const staleAfter = 6 * time.Hour

// chainPrefix names the migrated template every binary of one migration
// chain shares, followed by the chain's digest. Its comment holds when a
// binary last used it, in Unix seconds.
const chainPrefix = "template_chain_"

var swept sync.Once //nolint:gochecknoglobals // One sweep per test binary.

// New creates an empty migrated database, returns a pool on it and drops it
// when the test ends. Set LAZYCLOUD_TEST_DATABASE_URL to use another server.
func New(t testing.TB) *pgxpool.Pool {
	t.Helper()
	base, admin := connect(t)
	defer func() { _ = admin.Close(context.WithoutCancel(t.Context())) }()
	shared.once.Do(func() { shared.name, shared.err = migrateTemplate(t.Context(), admin, base) })
	if shared.err != nil {
		t.Fatalf("migrate template database: %v", shared.err)
	}
	return clone(t, base, admin, shared.name)
}

// prepared are the templates NewPrepared filled, by key.
var prepared = struct { //nolint:gochecknoglobals // One filled template per key and test binary.
	sync.Mutex
	templates map[string]*template
}{templates: map[string]*template{}}

// NewPrepared is New over a database that prepare filled once per test
// binary and key, such as with work every test of a suite would repeat.
// prepare's pool closes before the first clone.
func NewPrepared(t testing.TB, key string, prepare func(context.Context, *pgxpool.Pool) error) *pgxpool.Pool {
	t.Helper()
	base, admin := connect(t)
	defer func() { _ = admin.Close(context.WithoutCancel(t.Context())) }()
	shared.once.Do(func() { shared.name, shared.err = migrateTemplate(t.Context(), admin, base) })
	if shared.err != nil {
		t.Fatalf("migrate template database: %v", shared.err)
	}
	prepared.Lock()
	tmpl, ok := prepared.templates[key]
	if !ok {
		tmpl = &template{}
		prepared.templates[key] = tmpl
	}
	prepared.Unlock()
	tmpl.once.Do(func() {
		tmpl.name = "template_" + randomSuffix()
		if _, tmpl.err = admin.Exec(t.Context(), fmt.Sprintf("create database %s template %s", tmpl.name, shared.name)); tmpl.err != nil {
			return
		}
		pool, err := database.Open(t.Context(), withDatabase(base, tmpl.name))
		if err != nil {
			tmpl.err = err
			return
		}
		defer pool.Close()
		tmpl.err = prepare(t.Context(), pool)
	})
	if tmpl.err != nil {
		t.Fatalf("prepare template database %s: %v", key, tmpl.err)
	}
	return clone(t, base, admin, tmpl.name)
}

// connect returns the server's URL and an admin connection to it.
func connect(t testing.TB) (string, *pgx.Conn) {
	t.Helper()
	base := os.Getenv("LAZYCLOUD_TEST_DATABASE_URL")
	if base == "" {
		base = defaultURL
	}
	admin, err := pgx.Connect(t.Context(), base)
	if err != nil {
		t.Fatalf("connect to test postgres (docker compose -f compose.test.yaml up -d --wait): %v", err)
	}
	swept.Do(func() { sweep(t, admin) })
	return base, admin
}

// sweep drops the databases earlier binaries made more than staleAfter ago
// and the chain templates unused as long.
func sweep(t testing.TB, admin *pgx.Conn) {
	t.Helper()
	rows, err := admin.Query(t.Context(), "select datname from pg_database where datname ~ '^(template|test)_[0-9]+_'")
	if err != nil {
		t.Fatalf("list test databases: %v", err)
	}
	names, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		t.Fatalf("list test databases: %v", err)
	}
	cutoff := time.Now().Add(-staleAfter).Unix()
	for _, name := range names {
		made, err := strconv.ParseInt(strings.Split(name, "_")[1], 10, 64)
		if err != nil || made >= cutoff {
			continue
		}
		if _, err := admin.Exec(t.Context(), fmt.Sprintf("drop database if exists %s with (force)", name)); err != nil {
			t.Fatalf("drop stale test database %s: %v", name, err)
		}
	}
	rows, err = admin.Query(t.Context(), "select datname from pg_database where starts_with(datname, $1)", chainPrefix)
	if err != nil {
		t.Fatalf("list chain templates: %v", err)
	}
	chains, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		t.Fatalf("list chain templates: %v", err)
	}
	for _, name := range chains {
		if err := dropUnused(t.Context(), admin, name, cutoff); err != nil {
			t.Fatalf("drop unused chain template %s: %v", name, err)
		}
	}
}

// dropUnused drops chain template name if no binary used it since cutoff,
// holding the lock a binary takes to use it.
func dropUnused(ctx context.Context, admin *pgx.Conn, name string, cutoff int64) error {
	unlock, err := lockChain(ctx, admin, name)
	if err != nil {
		return err
	}
	defer unlock()
	var used *string
	if err := admin.QueryRow(ctx, "select shobj_description(oid, 'pg_database') from pg_database where datname = $1", name).Scan(&used); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		return fmt.Errorf("read last use: %w", err)
	}
	if used != nil {
		if at, err := strconv.ParseInt(*used, 10, 64); err == nil && at >= cutoff {
			return nil
		}
	}
	if _, err := admin.Exec(ctx, fmt.Sprintf("drop database if exists %s with (force)", name)); err != nil {
		return fmt.Errorf("drop: %w", err)
	}
	return nil
}

// lockChain takes the session lock that serializes making, using and
// dropping chain template name, and returns its release.
func lockChain(ctx context.Context, admin *pgx.Conn, name string) (func(), error) {
	if _, err := admin.Exec(ctx, "select pg_advisory_lock(hashtext($1))", name); err != nil {
		return nil, fmt.Errorf("lock %s: %w", name, err)
	}
	return func() { _, _ = admin.Exec(context.WithoutCancel(ctx), "select pg_advisory_unlock(hashtext($1))", name) }, nil
}

// clone creates a database from template, returns a pool on it and drops
// it when the test ends.
func clone(t testing.TB, base string, admin *pgx.Conn, template string) *pgxpool.Pool {
	t.Helper()
	ctx := t.Context()
	name := "test_" + randomSuffix()
	if _, err := admin.Exec(ctx, fmt.Sprintf("create database %s template %s", name, template)); err != nil {
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

// migrateTemplate returns the template of this tree's migration chain,
// which every binary of the chain shares, and records this use for sweep.
// The first binary migrates it under a sweepable name and publishes it by
// renaming, so no binary clones a half-migrated template.
func migrateTemplate(ctx context.Context, admin *pgx.Conn, base string) (string, error) {
	digest, err := chainDigest()
	if err != nil {
		return "", err
	}
	name := chainPrefix + digest
	unlock, err := lockChain(ctx, admin, name)
	if err != nil {
		return "", err
	}
	defer unlock()
	var exists bool
	if err := admin.QueryRow(ctx, "select exists (select from pg_database where datname = $1)", name).Scan(&exists); err != nil {
		return "", fmt.Errorf("find template: %w", err)
	}
	if !exists {
		building := "template_" + randomSuffix()
		if _, err := admin.Exec(ctx, "create database "+building); err != nil {
			return "", fmt.Errorf("create template: %w", err)
		}
		pool, err := database.Open(ctx, withDatabase(base, building))
		if err != nil {
			return "", err
		}
		err = database.Migrate(ctx, pool)
		pool.Close()
		if err != nil {
			return "", err
		}
		if _, err := admin.Exec(ctx, fmt.Sprintf("alter database %s rename to %s", building, name)); err != nil {
			return "", fmt.Errorf("publish template: %w", err)
		}
	}
	if _, err := admin.Exec(ctx, fmt.Sprintf("comment on database %s is '%d'", name, time.Now().Unix())); err != nil {
		return "", fmt.Errorf("record template use: %w", err)
	}
	return name, nil
}

// chainDigest identifies the migration chain by its files' names and
// contents.
func chainDigest() (string, error) {
	h := sha256.New()
	err := fs.WalkDir(migrations.Files, ".", func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		data, err := fs.ReadFile(migrations.Files, path)
		if err != nil {
			return fmt.Errorf("read %s: %w", path, err)
		}
		h.Write([]byte(path + "\x00" + strconv.Itoa(len(data)) + "\x00"))
		h.Write(data)
		return nil
	})
	if err != nil {
		return "", fmt.Errorf("read migrations: %w", err)
	}
	return hex.EncodeToString(h.Sum(nil))[:16], nil
}

// withDatabase is base on database name, with production's four
// connections per pool so parallel tests and packages stay within the
// server's max_connections.
func withDatabase(base, name string) string {
	u, err := url.Parse(base)
	if err != nil {
		return base
	}
	u.Path = "/" + name
	query := u.Query()
	query.Set("pool_max_conns", "4")
	u.RawQuery = query.Encode()
	return u.String()
}

// randomSuffix names a database by when it was made, for sweep.
func randomSuffix() string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return strconv.FormatInt(time.Now().Unix(), 10) + "_" + hex.EncodeToString(b[:])
}
