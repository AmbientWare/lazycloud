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

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// After shutdown starts the server reports draining on /readyz and keeps
// serving for the drain delay, so a load balancer stops routing to it
// before its listeners close; then it stops within the shutdown grace.
func TestServeDrainsOnShutdown(t *testing.T) {
	pool := dbtest.New(t)
	key := filepath.Join(t.TempDir(), "secrets.key")
	secret := make([]byte, 32)
	_, _ = rand.Read(secret)
	if err := os.WriteFile(key, secret, 0o600); err != nil {
		t.Fatal(err)
	}
	cfg := serveConfig{objectStore: storagetest.Config(), secretsKey: key, drainDelay: time.Second}
	cfg.images.Registry = "127.0.0.1:1"
	httpListener := listen(t)
	grpcListener := listen(t)
	base := "http://" + httpListener.Addr().String()

	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- serveWith(ctx, pool, cfg, slog.New(slog.DiscardHandler), httpListener, grpcListener)
	}()

	waitFor(t, base+"/readyz", http.StatusOK)
	if status := get(t, base+"/healthz"); status != http.StatusOK {
		t.Fatalf("healthz = %d, want 200", status)
	}

	stopped := time.Now()
	cancel()
	waitFor(t, base+"/readyz", http.StatusServiceUnavailable)
	// Still serving during the drain delay.
	if status := get(t, base+"/healthz"); status != http.StatusOK {
		t.Fatalf("healthz while draining = %d, want 200", status)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("serve: %v", err)
		}
	case <-time.After(cfg.drainDelay + shutdownGrace):
		t.Fatal("server did not stop within the drain delay and shutdown grace")
	}
	if elapsed := time.Since(stopped); elapsed < cfg.drainDelay {
		t.Fatalf("server stopped after %s, before the %s drain delay", elapsed, cfg.drainDelay)
	}
	var dialer net.Dialer
	if conn, err := dialer.DialContext(t.Context(), "tcp", httpListener.Addr().String()); err == nil {
		_ = conn.Close()
		t.Fatal("listener still accepts after shutdown")
	}
}

func listen(t *testing.T) net.Listener {
	t.Helper()
	var lc net.ListenConfig
	l, err := lc.Listen(t.Context(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	return l
}

func get(t *testing.T, url string) int {
	t.Helper()
	req, err := http.NewRequestWithContext(context.WithoutCancel(t.Context()), http.MethodGet, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	return resp.StatusCode
}

// waitFor polls url until it answers status, for up to 10 seconds.
func waitFor(t *testing.T, url string, status int) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		req, err := http.NewRequestWithContext(context.WithoutCancel(t.Context()), http.MethodGet, url, nil)
		if err != nil {
			t.Fatal(err)
		}
		resp, err := http.DefaultClient.Do(req)
		if err == nil {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if resp.StatusCode == status {
				return
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("GET %s did not return %d within 10s (last error %v)", url, status, err)
		}
		time.Sleep(20 * time.Millisecond)
	}
}
