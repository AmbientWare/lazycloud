package storage

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"regexp"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	awshttp "github.com/aws/aws-sdk-go-v2/aws/transport/http"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/smithy-go"
	smithyhttp "github.com/aws/smithy-go/transport/http"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// TestBucketNamesFitS3: the longest prefix a connection role allows, a
// 12-digit account id and any workspace id make a valid S3 bucket name
// within 63 characters, distinct for every account and workspace.
func TestBucketNamesFitS3(t *testing.T) {
	prefix := strings.Repeat("p", maxPrefix)
	if !prefixPattern.MatchString(prefix) || prefixPattern.MatchString(prefix+"p") {
		t.Fatalf("prefix pattern does not stop at %d characters", maxPrefix)
	}
	var highest identity.WorkspaceID
	for n := range highest {
		highest[n] = 0xff
	}
	valid := regexp.MustCompile(`^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$`)
	seen := map[string]bool{}
	for _, account := range []string{"123456789012", "210987654321"} {
		for _, ws := range []identity.WorkspaceID{{}, {1}, highest} {
			name := bucketName(prefix, account, ws)
			if !valid.MatchString(name) || seen[name] {
				t.Errorf("bucket %q (%d characters) is invalid or taken", name, len(name))
			}
			seen[name] = true
		}
	}
}

// TestHostPolicyReachesOnlyVolumeAndDiskObjects: an AWS host grant can read
// and write objects under volumes/ and disks/ and nothing else, lists only
// those prefixes, and lists multipart uploads, which S3 conditions on no
// prefix, without one. Each holds only on a bucket the expected account
// owns, so a bucket of the name in another account is out of reach.
func TestHostPolicyReachesOnlyVolumeAndDiskObjects(t *testing.T) {
	encoded, err := json.Marshal(hostPolicy("lc-ws-1", "123456789012"))
	if err != nil {
		t.Fatal(err)
	}
	policy := string(encoded)
	for _, banned := range []string{`"s3:*"`, `"*"`, "DeleteBucket", "PutBucket", `"arn:aws:s3:::lc-ws-1/*"`} {
		if strings.Contains(policy, banned) {
			t.Errorf("policy holds %s: %s", banned, policy)
		}
	}
	for _, want := range []string{"arn:aws:s3:::lc-ws-1/volumes/*", "arn:aws:s3:::lc-ws-1/disks/*"} {
		if !strings.Contains(policy, want) {
			t.Errorf("policy lacks %s: %s", want, policy)
		}
	}
	var parsed struct {
		Statement []struct {
			Action    []string
			Condition struct {
				StringEquals map[string]string
				StringLike   map[string][]string
			}
		}
	}
	if err := json.Unmarshal(encoded, &parsed); err != nil {
		t.Fatal(err)
	}
	prefixes := map[string][]string{}
	for _, s := range parsed.Statement {
		if owner := s.Condition.StringEquals["s3:ResourceAccount"]; owner != "123456789012" || len(s.Condition.StringEquals) != 1 {
			t.Errorf("%v holds on buckets owned by %q, want only 123456789012's", s.Action, owner)
		}
		for _, action := range s.Action {
			prefixes[action] = s.Condition.StringLike["s3:prefix"]
		}
	}
	if got := prefixes["s3:ListBucket"]; !slices.Equal(got, []string{"volumes/*", "disks/*"}) {
		t.Errorf("ListBucket conditioned on prefixes %v, want volumes/ and disks/", got)
	}
	if got, ok := prefixes["s3:ListBucketMultipartUploads"]; !ok || got != nil {
		t.Errorf("ListBucketMultipartUploads granted %v on prefixes %v, want granted on none", ok, got)
	}
}

// TestRequestsNameTheBucketOwner: every request of a client that expects
// an owner names it, presigned URLs included, except CreateBucket, which
// S3 does not accept it on.
func TestRequestsNameTheBucketOwner(t *testing.T) {
	var mu sync.Mutex
	owners := map[string]string{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		owners[r.Method] = r.Header.Get("X-Amz-Expected-Bucket-Owner")
		mu.Unlock()
	}))
	defer server.Close()
	client := s3.New(s3.Options{
		Region: "us-east-2", BaseEndpoint: aws.String(server.URL), UsePathStyle: true,
		Credentials: credentials.NewStaticCredentialsProvider("AKID", "secret", ""),
	}, expectOwner("123456789012"))
	ctx := t.Context()
	if _, err := client.CreateBucket(ctx, &s3.CreateBucketInput{Bucket: aws.String("b")}); err != nil {
		t.Fatal(err)
	}
	if _, err := client.DeleteObject(ctx, &s3.DeleteObjectInput{Bucket: aws.String("b"), Key: aws.String("k")}); err != nil {
		t.Fatal(err)
	}
	if owners[http.MethodPut] != "" || owners[http.MethodDelete] != "123456789012" {
		t.Fatalf("CreateBucket named owner %q and DeleteObject %q, want none and 123456789012", owners[http.MethodPut], owners[http.MethodDelete])
	}
	presigned, err := s3.NewPresignClient(client).PresignGetObject(ctx, &s3.GetObjectInput{Bucket: aws.String("b"), Key: aws.String("k")})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(presigned.URL, "x-amz-expected-bucket-owner=123456789012") {
		t.Fatalf("presigned URL %s names no owner", presigned.URL)
	}
}

// TestBucketsReachAnExplicitEndpoint: a bucket without an endpoint is AWS S3
// in its region, addressed by subdomain unless forced path-style or its
// name holds a dot; deploy refuses an AWS bucket without a region.
func TestBucketsReachAnExplicitEndpoint(t *testing.T) {
	cases := []struct {
		endpoint, region, bucket string
		force                    bool
		want                     Location
	}{
		{"", "us-east-2", "lc-ws-1", false, Location{Endpoint: "https://s3.us-east-2.amazonaws.com", Region: "us-east-2", Bucket: "lc-ws-1"}},
		{"", "us-east-2", "lc-ws-1", true, Location{Endpoint: "https://s3.us-east-2.amazonaws.com", Region: "us-east-2", Bucket: "lc-ws-1", PathStyle: true}},
		{"", "us-east-2", "media.example.com", false, Location{Endpoint: "https://s3.us-east-2.amazonaws.com", Region: "us-east-2", Bucket: "media.example.com", PathStyle: true}},
		{"http://garage:3900", "garage", "lc-ws-1", true, Location{Endpoint: "http://garage:3900", Region: "garage", Bucket: "lc-ws-1", PathStyle: true}},
		{"https://r2.example.com", "auto", "models", false, Location{Endpoint: "https://r2.example.com", Region: "auto", Bucket: "models"}},
	}
	for _, c := range cases {
		got, err := locate(c.endpoint, c.region, c.bucket, c.force)
		if err != nil || got != c.want {
			t.Errorf("locate(%q, %q, %q, %v) = %+v, %v; want %+v", c.endpoint, c.region, c.bucket, c.force, got, err, c.want)
		}
	}
	bucket := "my-videos"
	noRegion := apitypes.VolumeMountSpec{Name: "videos", CloudBucket: &apitypes.CloudBucketSpec{
		Bucket: bucket, AccessKeySecret: &bucket, SecretKeySecret: &bucket,
	}}
	if err := ValidateVolumes([]apitypes.VolumeMountSpec{noRegion}); err == nil {
		t.Fatal("deployed an AWS bucket without a region")
	}
}

// TestOnlyTheStoresLastingRefusalsAreTyped: a 4xx the object store gives
// for good, such as access denied or a missing bucket, is a
// *StoreRefusedError with its code; throttling, a timeout, a 5xx or a
// failure to reach the store stays a plain error the caller retries.
func TestOnlyTheStoresLastingRefusalsAreTyped(t *testing.T) {
	aws := func(status int, code string) error {
		return fmt.Errorf("create workspace bucket: %w", &smithy.OperationError{ServiceID: "S3", OperationName: "CreateBucket", Err: &awshttp.ResponseError{
			ResponseError: &smithyhttp.ResponseError{
				Response: &smithyhttp.Response{Response: &http.Response{StatusCode: status}},
				Err:      &smithy.GenericAPIError{Code: code},
			},
		}})
	}
	for _, c := range []struct {
		err  error
		code string
	}{
		{aws(http.StatusForbidden, "AccessDenied"), "AccessDenied"},
		{aws(http.StatusBadRequest, "InvalidBucketName"), "InvalidBucketName"},
		{&garageError{Op: "GetBucketInfo", Status: http.StatusNotFound}, "HTTP 404"},
		{aws(http.StatusBadRequest, "Throttling"), ""},
		{aws(http.StatusTooManyRequests, "TooManyRequests"), ""},
		{aws(http.StatusBadRequest, "RequestTimeout"), ""},
		{aws(http.StatusServiceUnavailable, "SlowDown"), ""},
		{aws(http.StatusInternalServerError, "InternalError"), ""},
		{&garageError{Op: "CreateKey", Status: http.StatusInternalServerError}, ""},
		{errors.New("dial tcp: connection refused"), ""},
	} {
		got := storeError(c.err)
		var refused *StoreRefusedError
		typed := errors.As(got, &refused)
		switch {
		case !errors.Is(got, c.err):
			t.Errorf("%v: %v lost the store's error", c.err, got)
		case c.code == "" && typed:
			t.Errorf("%v: refused with code %s, want it returned for a retry", c.err, refused.Code)
		case c.code != "" && (!typed || refused.Code != c.code):
			t.Errorf("%v: %v, want a refusal with code %s", c.err, got, c.code)
		}
	}
}
