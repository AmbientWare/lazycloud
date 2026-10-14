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
	t.Parallel()
	var calls atomic.Int64
	var lifetime atomic.Int64
	// The response shape of ECR GetAuthorizationToken in the AWS API
	// reference: a base64 "AWS:<password>" token and its expiry in epoch
	// seconds.
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

	auth := func() string {
		t.Helper()
		a, err := login.auth(t.Context())
		if err != nil || a.Username != "AWS" {
			t.Fatalf("login %+v: %v", a, err)
		}
		return a.Password
	}
	// A token with less than ecrRefresh left is replaced on each use.
	lifetime.Store(int64(ecrRefresh - time.Minute))
	if first, second := auth(), auth(); first != "token-1" || second != "token-2" {
		t.Fatalf("short-lived logins %s then %s, want a new token each time", first, second)
	}
	// A fresh one is reused.
	lifetime.Store(int64(12 * time.Hour))
	if first, second := auth(), auth(); first != "token-3" || second != "token-3" || calls.Load() != 3 {
		t.Fatalf("fresh logins %s then %s after %d requests, want one new token reused", first, second, calls.Load())
	}
}
