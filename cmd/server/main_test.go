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
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// After shutdown starts the server reports draining on /readyz and keeps
// serving the API for the drain delay, so a load balancer stops routing to
// it before its listeners close; then it stops within the shutdown grace.
// Background work outlives the drain: a token used while draining is still
// recorded.
func TestServeDrainsOnShutdown(t *testing.T) {
	pool := dbtest.New(t)
	key := filepath.Join(t.TempDir(), "secrets.key")
	secret := make([]byte, 32)
	_, _ = rand.Read(secret)
	if err := os.WriteFile(key, secret, 0o600); err != nil {
		t.Fatal(err)
	}
	ident := identity.NewIdentity(pool, identity.Config{})
	if _, err := ident.CreateUser(t.Context(), "drain@example.com", false); err != nil {
		t.Fatal(err)
	}
	token, err := ident.CreateToken(t.Context(), "drain@example.com", "", "drain")
	if err != nil {
		t.Fatal(err)
	}
	cfg := serveConfig{objectStore: storagetest.Config(), secretsKey: key, drainDelay: time.Second}
	cfg.images.Registry = "127.0.0.1:1"
	ls := listeners{http: listen(t), grpc: listen(t), health: listen(t), edge: listen(t), relay: listen(t)}
	cfg.edgeURL, cfg.relayURL = "http://lazycloud.localhost:8082", ls.relay.Addr().String()
	api := "http://" + ls.http.Addr().String()
	probes := "http://" + ls.health.Addr().String()

	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	tel, err := telemetry.New(t.Context(), telemetry.Config{Service: "server"})
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() {
		done <- serveWith(ctx, pool, pool, cfg, tel, slog.New(slog.DiscardHandler), ls)
	}()

	waitFor(t, probes+"/readyz", http.StatusOK)
	if status := get(t, probes+"/healthz", ""); status != http.StatusOK {
		t.Fatalf("healthz = %d, want 200", status)
	}
	if status := get(t, api+"/readyz", ""); status == http.StatusOK {
		t.Fatal("the API port answers /readyz; probes belong on the health port")
	}

	stopped := time.Now()
	cancel()
	waitFor(t, probes+"/readyz", http.StatusServiceUnavailable)
	// Still serving during the drain delay.
	if status := get(t, api+"/v1/me", token); status != http.StatusOK {
		t.Fatalf("API while draining = %d, want 200", status)
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
	for _, l := range []net.Listener{ls.http, ls.health} {
		if conn, err := dialer.DialContext(t.Context(), "tcp", l.Addr().String()); err == nil {
			_ = conn.Close()
			t.Fatalf("%s still accepts after shutdown", l.Addr())
		}
	}
	var used *time.Time
	if err := pool.QueryRow(t.Context(), "select last_used_at from api_tokens where name = 'drain'").Scan(&used); err != nil || used == nil {
		t.Fatalf("token use during the drain was not recorded: %v %v", used, err)
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

// get returns the status of GET url, with token as the bearer when set.
func get(t *testing.T, url, token string) int {
	t.Helper()
	req, err := http.NewRequestWithContext(context.WithoutCancel(t.Context()), http.MethodGet, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
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
