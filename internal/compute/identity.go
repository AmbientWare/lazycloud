package compute

import (
	"bytes"
	"context"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// HostIDHeader is the header an instance signs into its identity proof, so
// the proof names one host.
const HostIDHeader = "Lazycloud-Host-Id"

// IdentityProof is an STS GetCallerIdentity request an instance signed with
// its instance-profile credentials.
type IdentityProof struct {
	URL     string
	Method  string
	Headers map[string]string
	Body    []byte
}

// callerIdentity is what STS answered for a proof.
type callerIdentity struct {
	Account string `xml:"GetCallerIdentityResult>Account"`
	Arn     string `xml:"GetCallerIdentityResult>Arn"`
	UserID  string `xml:"GetCallerIdentityResult>UserId"`
}

var (
	stsHost        = regexp.MustCompile(`^sts(\.[a-z]{2}(-gov)?-[a-z]+-[0-9]+)?\.amazonaws\.com$`)
	assumedRoleArn = regexp.MustCompile(`^arn:aws:sts::([0-9]{12}):assumed-role/([A-Za-z0-9+=,.@_-]+)/(i-[0-9a-f]{8,32})$`)
	roleArn        = regexp.MustCompile(`^arn:aws:iam::([0-9]{12}):role/(?:[A-Za-z0-9+=,.@_/-]*/)?([A-Za-z0-9+=,.@_-]+)$`)
)

// maxProofResponse bounds what the server reads from STS.
const maxProofResponse = 32 << 10

// EnrollCloud issues a host token to the instance launched for host once
// its identity proof checks out: STS confirms the caller is the node role
// of the host's account in a session named for the host's instance. The
// proof names the host in a signed header, so it cannot enroll another
// host, and a host enrolls once.
func (c *Compute) EnrollCloud(ctx context.Context, host HostID, proof IdentityProof, report HostReport) (HostID, string, error) {
	row, err := c.queries.CloudHostIdentity(ctx, uuid.UUID(host))
	if errors.Is(err, pgx.ErrNoRows) {
		return HostID{}, "", &IdentityError{Message: "the host is not a launched instance"}
	}
	if err != nil {
		return HostID{}, "", fmt.Errorf("read host identity: %w", err)
	}
	if row.InstanceID == nil {
		return HostID{}, "", &IdentityError{Message: "the host has no instance yet"}
	}
	// The role the host launched with; platform hosts run as the fleet's.
	account, role := c.fleet.AccountID, deref(row.NodeRoleArn)
	if HostKind(row.Kind) == KindConnection {
		account = deref(row.AwsAccountID)
	} else if role == "" {
		role = c.fleet.NodeRoleARN
	}
	caller, err := c.verifyProof(ctx, host, proof)
	if err != nil {
		return HostID{}, "", err
	}
	m := assumedRoleArn.FindStringSubmatch(caller.Arn)
	if m == nil {
		return HostID{}, "", &IdentityError{Message: "the proof is not from an instance role session"}
	}
	wantRole := ""
	if r := roleArn.FindStringSubmatch(role); r != nil && r[1] == account {
		wantRole = r[2]
	}
	if m[1] != account || caller.Account != account || m[2] != wantRole || m[3] != *row.InstanceID ||
		!strings.HasSuffix(caller.UserID, ":"+*row.InstanceID) {
		return HostID{}, "", &IdentityError{Message: "the proof does not match the host's instance and role"}
	}
	if failed := failedChecks(report.Preflight); len(failed) > 0 {
		// The instance cannot serve; reconciliation terminates it as an
		// orphan of a failed host.
		message := strings.Join(failed, "; ")
		if _, err := c.queries.FailHost(ctx, FailHostParams{
			ID: uuid.UUID(host), FromPhase: row.Phase, Failure: ptr(string(FailurePreflight)), Message: truncate(message),
		}); err != nil {
			return HostID{}, "", fmt.Errorf("fail host: %w", err)
		}
		return HostID{}, "", &IdentityError{Message: "the host failed its preflight checks: " + message}
	}
	token, digest, err := identity.NewToken()
	if err != nil {
		return HostID{}, "", err
	}
	preflight, err := json.Marshal(nonNil(report.Preflight))
	if err != nil {
		return HostID{}, "", fmt.Errorf("encode preflight: %w", err)
	}
	err = pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		_, err := c.queries.WithTx(tx).EnrollCloudHost(ctx, EnrollCloudHostParams{
			ID: uuid.UUID(host), InstanceID: row.InstanceID, TokenHash: digest,
			CpuMillis: report.Capacity.CPUMillis, MemoryBytes: report.Capacity.MemoryBytes,
			GpuType: report.Capacity.GPUType, GpuCount: int32(report.Capacity.GPUCount), //nolint:gosec // GPU counts are small.
			Architecture: report.architecture(), Preflight: preflight,
		})
		if errors.Is(err, pgx.ErrNoRows) {
			return &IdentityError{Message: "the host already enrolled or stopped waiting for its instance"}
		}
		if err != nil {
			return fmt.Errorf("enroll cloud host: %w", err)
		}
		return notifyMachines(ctx, tx, uuid.UUID(host))
	})
	if err != nil {
		return HostID{}, "", fmt.Errorf("enroll cloud host: %w", err)
	}
	return host, token, nil
}

// verifyProof checks that the proof is a GetCallerIdentity request to STS
// that signs the host header, sends it and returns who STS says signed it.
func (c *Compute) verifyProof(ctx context.Context, host HostID, proof IdentityProof) (callerIdentity, error) {
	u, err := url.Parse(proof.URL)
	if err != nil || u.Scheme != "https" || !stsHost.MatchString(u.Hostname()) || u.Port() != "" || u.User != nil {
		return callerIdentity{}, &IdentityError{Message: "the proof must target AWS STS over HTTPS"}
	}
	if len(proof.URL) > 16<<10 || len(proof.Body) > 16<<10 {
		return callerIdentity{}, &IdentityError{Message: "the proof is too large"}
	}
	method := strings.ToUpper(proof.Method)
	if method != http.MethodGet && method != http.MethodPost {
		return callerIdentity{}, &IdentityError{Message: "the proof must be a GET or POST"}
	}
	params := u.Query()
	if method == http.MethodPost {
		body, err := url.ParseQuery(string(proof.Body))
		if err != nil {
			return callerIdentity{}, &IdentityError{Message: "the proof body is not a form"}
		}
		for k, v := range body {
			params[k] = v
		}
	}
	if params.Get("Action") != "GetCallerIdentity" || params.Get("Version") != "2011-06-15" {
		return callerIdentity{}, &IdentityError{Message: "the proof must call GetCallerIdentity"}
	}
	if expires := params.Get("X-Amz-Expires"); expires != "" {
		if n, err := strconv.Atoi(expires); err != nil || n < 1 || n > 900 {
			return callerIdentity{}, &IdentityError{Message: "the proof expires too late"}
		}
	}
	headers := http.Header{}
	for k, v := range proof.Headers {
		headers.Set(k, v)
	}
	if headers.Get(HostIDHeader) != host.String() {
		return callerIdentity{}, &IdentityError{Message: "the proof names another host"}
	}
	signed := params.Get("X-Amz-SignedHeaders")
	if signed == "" {
		signed = signedHeadersOf(headers.Get("Authorization"))
	}
	if !containsFold(strings.Split(signed, ";"), HostIDHeader) {
		return callerIdentity{}, &IdentityError{Message: "the proof does not sign the host header"}
	}
	target := proof.URL
	if c.fleet.Endpoints.STS != "" {
		target = strings.TrimRight(c.fleet.Endpoints.STS, "/") + u.RequestURI()
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, target, bytes.NewReader(proof.Body))
	if err != nil {
		return callerIdentity{}, &IdentityError{Message: "the proof is not a valid request"}
	}
	req.Header = headers
	req.Host = u.Host
	resp, err := c.http.Do(req)
	if err != nil {
		return callerIdentity{}, &UnavailableError{Message: fmt.Sprintf("AWS STS is unavailable: %v", err)}
	}
	defer func() { _ = resp.Body.Close() }()
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxProofResponse))
	if err != nil {
		return callerIdentity{}, &UnavailableError{Message: fmt.Sprintf("read AWS STS response: %v", err)}
	}
	if resp.StatusCode != http.StatusOK {
		return callerIdentity{}, &IdentityError{Message: fmt.Sprintf("AWS STS refused the proof (HTTP %d)", resp.StatusCode)}
	}
	var out callerIdentity
	if err := xml.Unmarshal(data, &out); err != nil || out.Arn == "" {
		return callerIdentity{}, &IdentityError{Message: "AWS STS returned no caller identity"}
	}
	return out, nil
}

// signedHeadersOf reads SignedHeaders from a SigV4 Authorization header.
func signedHeadersOf(authorization string) string {
	for _, part := range strings.Split(authorization, ",") {
		part = strings.TrimSpace(part)
		if i := strings.Index(part, "SignedHeaders="); i >= 0 {
			return part[i+len("SignedHeaders="):]
		}
	}
	return ""
}

func containsFold(list []string, s string) bool {
	for _, v := range list {
		if strings.EqualFold(strings.TrimSpace(v), s) {
			return true
		}
	}
	return false
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
