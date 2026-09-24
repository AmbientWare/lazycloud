package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestStoreCredentialsRereadNearExpiry(t *testing.T) {
	path := filepath.Join(t.TempDir(), "store.json")
	start := time.Date(2026, 9, 23, 12, 0, 0, 0, time.UTC)
	write := func(key string, expires time.Time) {
		data, err := json.Marshal(storeConfig{
			Region: "us-east-2", Bucket: "b", AccessKey: key, SecretKey: "s", ExpiresAt: &expires,
		})
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, data, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	now := start
	provider := &storeCredentials{path: path, now: func() time.Time { return now }}
	retrieve := func() string {
		t.Helper()
		credentials, err := provider.Retrieve(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		return credentials.AccessKeyID
	}

	write("first", start.Add(15*time.Minute))
	if got := retrieve(); got != "first" {
		t.Fatalf("got %q, want first", got)
	}
	write("second", start.Add(30*time.Minute))
	now = start.Add(time.Minute)
	if got := retrieve(); got != "first" {
		t.Fatalf("re-read %q while the held credentials were far from expiry", got)
	}
	now = start.Add(15*time.Minute - storeCredentialMargin)
	if got := retrieve(); got != "second" {
		t.Fatalf("got %q within the margin, want the replaced second", got)
	}

	now = start.Add(30 * time.Minute)
	_, err := provider.Retrieve(context.Background())
	if err == nil || !strings.Contains(err.Error(), "did not replace them") {
		t.Fatalf("expired credentials the worker never replaced returned %v", err)
	}
}
