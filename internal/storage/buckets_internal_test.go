package storage

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// TestHostPolicyReachesOnlyVolumeAndDiskObjects: an AWS host grant can read
// and write objects under volumes/ and disks/ and nothing else, lists only
// those prefixes, and lists multipart uploads, which S3 conditions on no
// prefix, unconditionally.
func TestHostPolicyReachesOnlyVolumeAndDiskObjects(t *testing.T) {
	encoded, err := json.Marshal(hostPolicy("lc-ws-1"))
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
			Condition map[string]map[string][]string
		}
	}
	if err := json.Unmarshal(encoded, &parsed); err != nil {
		t.Fatal(err)
	}
	conditions := map[string]map[string]map[string][]string{}
	for _, s := range parsed.Statement {
		for _, action := range s.Action {
			conditions[action] = s.Condition
		}
	}
	if got := conditions["s3:ListBucket"]["StringLike"]["s3:prefix"]; !slices.Equal(got, []string{"volumes/*", "disks/*"}) {
		t.Errorf("ListBucket conditioned on %v, want the volumes/ and disks/ prefixes", conditions["s3:ListBucket"])
	}
	if c, ok := conditions["s3:ListBucketMultipartUploads"]; !ok || c != nil {
		t.Errorf("ListBucketMultipartUploads granted %v with condition %v, want granted without one", ok, c)
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
