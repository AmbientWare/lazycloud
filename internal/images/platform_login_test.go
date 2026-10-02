package images

import (
	"encoding/base64"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
)

// The platform's ECR login is minted from AWS credentials, reused while it
// has more than ecrRefresh left and replaced before it expires.
func TestPlatformECRLoginIsMintedAndReplacedBeforeItExpires(t *testing.T) {
	var calls atomic.Int64
	var lifetime atomic.Int64
	lifetime.Store(int64(12 * time.Hour))
	ecr := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Amz-Target") != "AmazonEC2ContainerRegistry_V20150921.GetAuthorizationToken" {
			http.Error(w, "unexpected call", http.StatusBadRequest)
			return
		}
		n := calls.Add(1)
		token := base64.StdEncoding.EncodeToString(fmt.Appendf(nil, "AWS:token-%d", n))
		expires := time.Now().Add(time.Duration(lifetime.Load())).Unix()
		w.Header().Set("Content-Type", "application/x-amz-json-1.1")
		_, _ = fmt.Fprintf(w, `{"authorizationData":[{"authorizationToken":%q,"expiresAt":%d}]}`, token, expires)
	}))
	defer ecr.Close()
	login := newPlatformLogin(Config{
		Registry: "123456789012.dkr.ecr.us-east-2.amazonaws.com",
		ECR: &aws.Config{
			BaseEndpoint: aws.String(ecr.URL),
			Credentials:  credentials.NewStaticCredentialsProvider("AKIA", "secret", ""),
			Retryer:      func() aws.Retryer { return aws.NopRetryer{} },
		},
	})

	for range 2 {
		auth, err := login.auth(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		if auth.Username != "AWS" || auth.Password != "token-1" {
			t.Fatalf("login %+v, want the first minted token", auth)
		}
	}
	if n := calls.Load(); n != 1 {
		t.Fatalf("%d token requests for a fresh login, want 1", n)
	}

	// A token with less than ecrRefresh left is replaced.
	lifetime.Store(int64(time.Hour))
	login.mu.Lock()
	login.expires = time.Now().Add(ecrRefresh - time.Minute)
	login.mu.Unlock()
	auth, err := login.auth(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if auth.Password != "token-2" || calls.Load() != 2 {
		t.Fatalf("login %+v after %d requests, want a second token", auth, calls.Load())
	}
}
