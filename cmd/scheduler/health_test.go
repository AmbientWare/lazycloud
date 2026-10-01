package main

import (
	"context"
	"crypto/rand"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// The scheduler is ready once every loop finished a pass, and stops its
// loops and health server on shutdown.
func TestSchedulerReadinessAndShutdown(t *testing.T) {
	pool := dbtest.New(t)
	addr := freeAddr(t)
	schedulerEnv(t, pool)
	t.Setenv("LAZYCLOUD_HEALTH_ADDR", addr)

	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- run(ctx, slog.New(slog.DiscardHandler)) }()

	base := "http://" + addr
	waitStatus(t, base+"/readyz", http.StatusOK)
	waitStatus(t, base+"/healthz", http.StatusOK)

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("run: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("scheduler did not stop within 10s of shutdown")
	}
	var dialer net.Dialer
	if conn, err := dialer.DialContext(t.Context(), "tcp", addr); err == nil {
		_ = conn.Close()
		t.Fatal("health server still accepts after shutdown")
	}
}

// A loop that stops finishing passes fails /healthz and /readyz.
func TestHeartbeatsReportStalledLoops(t *testing.T) {
	beats := newHeartbeats(func() bool { return true })
	pass := beats.track("placement", time.Second, func(context.Context) bool { return false })
	beats.track("planning", time.Second, func(context.Context) bool { return false })
	pass(t.Context())

	starting, stalled := beats.check(time.Now())
	if len(stalled) != 0 || len(starting) != 1 || starting[0] != "planning" {
		t.Fatalf("starting %v stalled %v, want planning starting", starting, stalled)
	}
	_, stalled = beats.check(time.Now().Add(time.Second + stallAllowance + time.Millisecond))
	if len(stalled) != 2 {
		t.Fatalf("stalled %v, want both loops", stalled)
	}
}

// A pass that runs long is cut off by its deadline and counts as finished,
// and progress within a pass is a heartbeat, so slow work never stalls a
// loop into a restart.
func TestPassesEndAtTheirDeadlineAndProgressBeats(t *testing.T) {
	beats := newHeartbeats(func() bool { return true })
	beats.passTimeout = 50 * time.Millisecond
	var beatDuringPass time.Time
	pass := beats.track("workspace deletion", time.Second, func(ctx context.Context) bool {
		mark := time.Now()
		progressed(ctx)
		beats.mu.Lock()
		beatDuringPass = beats.loops["workspace deletion"].since
		beats.mu.Unlock()
		if beatDuringPass.Before(mark) {
			t.Error("progress did not beat")
		}
		<-ctx.Done()
		return false
	})
	start := time.Now()
	pass(t.Context())
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Fatalf("pass ran %s past its 50ms deadline", elapsed)
	}
	if starting, stalled := beats.check(time.Now()); len(starting)+len(stalled) != 0 {
		t.Fatalf("starting %v stalled %v after a cut-off pass", starting, stalled)
	}
}

// schedulerEnv points the scheduler at pool, a fresh secrets key and the test
// object store, with email delivery off.
func schedulerEnv(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	key := filepath.Join(t.TempDir(), "secrets.key")
	secret := make([]byte, 32)
	_, _ = rand.Read(secret)
	if err := os.WriteFile(key, secret, 0o600); err != nil {
		t.Fatal(err)
	}
	store := storagetest.Config()
	for name, value := range map[string]string{
		"LAZYCLOUD_DATABASE_URL":                   pool.Config().ConnString(),
		"LAZYCLOUD_SECRETS_KEY_FILE":               key,
		"LAZYCLOUD_OBJECT_STORE_ENDPOINT":          store.Endpoint,
		"LAZYCLOUD_OBJECT_STORE_REGION":            store.Region,
		"LAZYCLOUD_OBJECT_STORE_BUCKET":            store.Bucket,
		"LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID":     store.AccessKeyID,
		"LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY": store.SecretAccessKey,
		"LAZYCLOUD_WORKSPACE_BUCKET_PROVIDER":      string(store.Workspaces.Provider),
		"LAZYCLOUD_WORKSPACE_BUCKET_PREFIX":        store.Workspaces.Prefix,
		"LAZYCLOUD_GARAGE_ADMIN_URL":               store.Workspaces.GarageAdminURL,
		"LAZYCLOUD_GARAGE_ADMIN_TOKEN":             store.Workspaces.GarageAdminToken,
		"LAZYCLOUD_RESEND_API_KEY":                 "",
	} {
		t.Setenv(name, value)
	}
}

func freeAddr(t *testing.T) string {
	t.Helper()
	var lc net.ListenConfig
	l, err := lc.Listen(t.Context(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}
	return addr
}

// waitStatus polls url until it answers status, for up to 15 seconds.
func waitStatus(t *testing.T, url string, status int) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for {
		req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, url, nil)
		if err != nil {
			t.Fatal(err)
		}
		resp, err := http.DefaultClient.Do(req)
		var body []byte
		if err == nil {
			body, _ = io.ReadAll(resp.Body)
			_ = resp.Body.Close()
			if resp.StatusCode == status {
				return
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("GET %s did not return %d within 15s (last error %v, body %q)", url, status, err, body)
		}
		time.Sleep(50 * time.Millisecond)
	}
}
