package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	v4 "github.com/aws/aws-sdk-go-v2/aws/signer/v4"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	testAccessKey = "ASIATESTACCESSKEY"
	testSecretKey = "test-secret-key"
	testSession   = "test-session-token"
	testRegion    = "us-east-2"
)

// imdsEmulator answers the IMDSv2 calls the agent makes: session tokens,
// the region, instance-profile credentials and the Spot notice.
type imdsEmulator struct {
	*httptest.Server
	mu      sync.Mutex
	tokens  map[string]bool
	minted  int
	notice  string
	refused int
}

func newIMDSEmulator(t *testing.T) *imdsEmulator {
	t.Helper()
	m := &imdsEmulator{tokens: map[string]bool{}}
	m.Server = httptest.NewServer(http.HandlerFunc(m.serve))
	t.Cleanup(m.Close)
	return m
}

func (m *imdsEmulator) serve(w http.ResponseWriter, r *http.Request) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if r.URL.Path == "/latest/api/token" {
		if r.Method != http.MethodPut || r.Header.Get("X-Aws-Ec2-Metadata-Token-Ttl-Seconds") == "" {
			http.Error(w, "token needs PUT with a TTL", http.StatusBadRequest)
			return
		}
		m.minted++
		token := fmt.Sprintf("token-%d", m.minted)
		m.tokens[token] = true
		w.Header().Set("X-Aws-Ec2-Metadata-Token-Ttl-Seconds", "21600")
		_, _ = w.Write([]byte(token))
		return
	}
	if !m.tokens[r.Header.Get("X-Aws-Ec2-Metadata-Token")] {
		m.refused++
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	switch r.URL.Path {
	case "/latest/meta-data/placement/region":
		_, _ = w.Write([]byte(testRegion))
	case "/latest/meta-data/iam/security-credentials/":
		_, _ = w.Write([]byte("lazycloud-node"))
	case "/latest/meta-data/iam/security-credentials/lazycloud-node":
		_ = json.NewEncoder(w).Encode(map[string]string{
			"Code": "Success", "Type": "AWS-HMAC", "AccessKeyId": testAccessKey, "SecretAccessKey": testSecretKey,
			"Token": testSession, "Expiration": time.Now().Add(6 * time.Hour).UTC().Format(time.RFC3339),
			"LastUpdated": time.Now().UTC().Format(time.RFC3339),
		})
	case "/latest/meta-data/spot/instance-action":
		if m.notice == "" {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write([]byte(m.notice))
	default:
		http.NotFound(w, r)
	}
}

// expireTokens invalidates every session token, as their TTL would.
func (m *imdsEmulator) expireTokens() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.tokens = map[string]bool{}
}

func (m *imdsEmulator) setNotice(action string, at time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.notice = fmt.Sprintf(`{"action": %q, "time": %q}`, action, at.UTC().Format(time.RFC3339))
}

func (m *imdsEmulator) refusals() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.refused
}

// presignedSignature recomputes the SigV4 query signature of a presigned
// request with the given host id header, the way STS validates it.
func presignedSignature(t *testing.T, identity *hostproto.CloudIdentity, hostID string) string {
	t.Helper()
	u, err := url.Parse(identity.GetUrl())
	if err != nil {
		t.Fatal(err)
	}
	query := u.Query()
	signedAt, err := time.Parse("20060102T150405Z", query.Get("X-Amz-Date"))
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date", "X-Amz-SignedHeaders", "X-Amz-Signature", "X-Amz-Security-Token"} {
		query.Del(key)
	}
	u.RawQuery = query.Encode()
	request, err := http.NewRequestWithContext(context.Background(), identity.GetMethod(), u.String(), nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set(hostIDHeader, hostID)
	credentials := aws.Credentials{AccessKeyID: testAccessKey, SecretAccessKey: testSecretKey, SessionToken: testSession}
	emptyBody := "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
	signed, _, err := v4.NewSigner().PresignHTTP(context.Background(), credentials, request, emptyBody, "sts", testRegion, signedAt)
	if err != nil {
		t.Fatal(err)
	}
	resigned, err := url.Parse(signed)
	if err != nil {
		t.Fatal(err)
	}
	return resigned.Query().Get("X-Amz-Signature")
}

func TestCloudIdentityIsAPresignedCallerIdentityRequest(t *testing.T) {
	metadata := newIMDSEmulator(t)
	hostID := uuid.NewString()
	identity, err := cloudIdentity(t.Context(), newIMDS(metadata.URL), hostID)
	if err != nil {
		t.Fatal(err)
	}
	u, err := url.Parse(identity.GetUrl())
	if err != nil {
		t.Fatal(err)
	}
	query := u.Query()
	if identity.GetHostId() != hostID || identity.GetMethod() != http.MethodGet || u.Host != "sts."+testRegion+".amazonaws.com" ||
		query.Get("Action") != "GetCallerIdentity" || query.Get("X-Amz-Expires") != "60" || query.Get("X-Amz-Security-Token") != testSession ||
		!strings.HasPrefix(query.Get("X-Amz-Credential"), testAccessKey+"/") || !strings.HasSuffix(query.Get("X-Amz-Credential"), "/"+testRegion+"/sts/aws4_request") {
		t.Fatalf("presigned request %s %s", identity.GetMethod(), identity.GetUrl())
	}
	if !slices.Contains(strings.Split(query.Get("X-Amz-SignedHeaders"), ";"), "lazycloud-host-id") {
		t.Fatalf("the signature does not cover the host id: %s", query.Get("X-Amz-SignedHeaders"))
	}
	if identity.GetHeaders()[http.CanonicalHeaderKey(hostIDHeader)] != hostID {
		t.Fatalf("headers %v", identity.GetHeaders())
	}
	if got := query.Get("X-Amz-Signature"); got == "" || got != presignedSignature(t, identity, hostID) {
		t.Fatalf("signature %q does not validate", got)
	}
	if presignedSignature(t, identity, uuid.NewString()) == query.Get("X-Amz-Signature") {
		t.Fatal("the signature validates for another host id")
	}
}

func TestCloudHostEnrollsAndReportsSpotInterruptions(t *testing.T) {
	metadata := newIMDSEmulator(t)
	e := newEnv(t)
	e.startAgent(func(cfg *Config) {
		cfg.JoinToken, cfg.CloudHostID, cfg.IMDSEndpoint = "", e.server.hostID, metadata.URL
	})
	enroll := e.enrollment()
	if enroll.GetJoinToken() != "" || enroll.GetCloudIdentity().GetHostId() != e.server.hostID || enroll.GetCloudIdentity().GetUrl() == "" {
		t.Fatalf("cloud enrollment %v", enroll)
	}
	s := e.session()

	// A notice arrives after the session token expired; the agent renews it.
	metadata.expireTokens()
	reclaimAt := time.Now().Add(2 * time.Minute).Truncate(time.Second)
	metadata.setNotice("terminate", reclaimAt)
	isInterruption := func(m *hostproto.HostMessage) bool { return m.GetInterruption() != nil }
	notice := s.until(t, 20*time.Second, isInterruption).GetInterruption()
	if notice.GetReason() != "aws-ec2-spot-terminate" || !notice.GetReclaimAt().AsTime().Equal(reclaimAt) {
		t.Fatalf("interruption %v", notice)
	}
	if metadata.refusals() == 0 {
		t.Fatal("the emulator never refused an expired token")
	}

	// A new session restates the standing notice right after Hello.
	e.grpc.Stop()
	e.grpc, _ = e.server.serve(t, e.address)
	s = e.session()
	if again := s.until(t, 10*time.Second, func(*hostproto.HostMessage) bool { return true }); again.GetInterruption().GetReason() != notice.GetReason() {
		t.Fatalf("first message after reconnect %v", again)
	}
}
