package storage

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

// isolate keeps the default chain from reading this machine's AWS files or
// instance metadata.
func isolate(t *testing.T) {
	t.Helper()
	dir := t.TempDir()
	for _, name := range []string{"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
		"AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
		"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"} {
		t.Setenv(name, "")
	}
	t.Setenv("AWS_CONFIG_FILE", filepath.Join(dir, "config"))
	t.Setenv("AWS_SHARED_CREDENTIALS_FILE", filepath.Join(dir, "credentials"))
	t.Setenv("AWS_EC2_METADATA_DISABLED", "true")
}

func TestStaticKeysWinOverTheDefaultChain(t *testing.T) {
	isolate(t)
	t.Setenv("AWS_ACCESS_KEY_ID", "from-env")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "env-secret")
	creds, err := credentialProvider(Config{Region: "garage", AccessKeyID: "static", SecretAccessKey: "static-secret"}).Retrieve(t.Context())
	if err != nil || creds.AccessKeyID != "static" || creds.SecretAccessKey != "static-secret" {
		t.Fatalf("static keys: %+v err=%v", creds, err)
	}
}

func TestDefaultChainReadsTheEnvironment(t *testing.T) {
	isolate(t)
	t.Setenv("AWS_ACCESS_KEY_ID", "from-env")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "env-secret")
	t.Setenv("AWS_SESSION_TOKEN", "env-token")
	creds, err := credentialProvider(Config{Region: "us-east-2"}).Retrieve(t.Context())
	if err != nil || creds.AccessKeyID != "from-env" || creds.SessionToken != "env-token" {
		t.Fatalf("default chain: %+v err=%v", creds, err)
	}
}

// TestDefaultChainUsesPodIdentity serves credentials the way the EKS Pod
// Identity agent does: a container credentials endpoint and a token file.
func TestDefaultChainUsesPodIdentity(t *testing.T) {
	isolate(t)
	expires := time.Now().Add(time.Hour).UTC().Format(time.RFC3339)
	endpoint := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "pod-token" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]string{
			"AccessKeyId": "from-pod", "SecretAccessKey": "pod-secret", "Token": "pod-session", "Expiration": expires,
		})
	}))
	t.Cleanup(endpoint.Close)
	token := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(token, []byte("pod-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", endpoint.URL+"/v1/credentials")
	t.Setenv("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", token)
	creds, err := credentialProvider(Config{Region: "us-east-2"}).Retrieve(t.Context())
	if err != nil || creds.AccessKeyID != "from-pod" || creds.SessionToken != "pod-session" {
		t.Fatalf("pod identity: %+v err=%v", creds, err)
	}
}

func TestDefaultChainWithoutSourceFails(t *testing.T) {
	isolate(t)
	if _, err := credentialProvider(Config{Region: "us-east-2"}).Retrieve(t.Context()); err == nil {
		t.Fatal("the default chain found credentials where none exist")
	}
}

func TestConfigValidation(t *testing.T) {
	ok := []Config{
		{Region: "us-east-2", Bucket: "b"},
		{Region: "us-east-2", Bucket: "b", Workspaces: WorkspaceBuckets{Provider: ProviderAWS, RoleARN: "arn:aws:iam::1:role/hosts"}},
		{Endpoint: "http://garage", Region: "garage", Bucket: "b", AccessKeyID: "k", SecretAccessKey: "s",
			Workspaces: WorkspaceBuckets{Provider: ProviderGarage}},
	}
	for _, c := range ok {
		if err := c.Validate(); err != nil {
			t.Errorf("%+v: %v", c, err)
		}
	}
	bad := []Config{
		{Bucket: "b"},
		{Region: "us-east-2", Bucket: "b", AccessKeyID: "k"},
		{Region: "garage", Bucket: "b", Workspaces: WorkspaceBuckets{Provider: ProviderGarage}},
		{Region: "us-east-2", Bucket: "b", Workspaces: WorkspaceBuckets{Provider: ProviderAWS}},
	}
	for _, c := range bad {
		if err := c.Validate(); err == nil {
			t.Errorf("%+v passed validation", c)
		}
	}
}

// TestSignedURLsOutlastCredentialRotation: when the chain's credentials are
// in their last minute, a presigned URL gets most of the window from renewed
// credentials, not a refusal, and never outlives the credentials that sign
// it.
func TestSignedURLsOutlastCredentialRotation(t *testing.T) {
	isolate(t)
	var mu sync.Mutex
	expiry := map[string]time.Time{}
	endpoint := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		key := fmt.Sprintf("key-%d", len(expiry))
		left := time.Hour
		if len(expiry) == 0 {
			left = 30 * time.Second
		}
		at := time.Now().Add(left).UTC().Truncate(time.Second)
		expiry[key] = at
		mu.Unlock()
		_ = json.NewEncoder(w).Encode(map[string]string{
			"AccessKeyId": key, "SecretAccessKey": "pod-secret", "Token": "pod-session", "Expiration": at.Format(time.RFC3339),
		})
	}))
	t.Cleanup(endpoint.Close)
	t.Setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", endpoint.URL+"/v1/credentials")
	s := NewStorage(nil, Config{Region: "us-east-2", Bucket: "b"})
	for range 2 {
		lifetime, err := s.signedLifetime(t.Context(), time.Hour)
		if err != nil {
			t.Fatal(err)
		}
		if lifetime < credentialWindow-2*time.Minute {
			t.Fatalf("a URL signed during rotation lasts %s", lifetime)
		}
		current, err := s.client.Options().Credentials.Retrieve(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		mu.Lock()
		expires := expiry[current.AccessKeyID]
		mu.Unlock()
		if time.Now().Add(lifetime).After(expires) {
			t.Fatalf("a URL lasting %s outlives its credentials, which expire %s", lifetime, expires)
		}
	}
}
