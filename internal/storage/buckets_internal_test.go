package storage

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// TestHostPolicyReachesOnlyVolumeAndDiskObjects: an AWS host grant can read
// and write objects under volumes/ and disks/ and nothing else.
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
	for _, want := range []string{"arn:aws:s3:::lc-ws-1/volumes/*", "arn:aws:s3:::lc-ws-1/disks/*", `"s3:prefix":["volumes/*","disks/*"]`} {
		if !strings.Contains(policy, want) {
			t.Errorf("policy lacks %s: %s", want, policy)
		}
	}
}

// TestBucketsReachAnExplicitEndpoint: every bucket address hosts receive
// names its endpoint, so their mount tools never fall back to a default of
// their own. An AWS bucket without a region is refused at deploy.
func TestBucketsReachAnExplicitEndpoint(t *testing.T) {
	aws, err := locate("", "us-east-2", "lc-ws-1", false)
	if err != nil || aws != (Location{Endpoint: "https://s3.us-east-2.amazonaws.com", Region: "us-east-2", Bucket: "lc-ws-1"}) {
		t.Fatalf("AWS bucket: %+v, %v", aws, err)
	}
	garage, err := locate("http://garage:3900", "garage", "lc-ws-1", false)
	if err != nil || garage.Endpoint != "http://garage:3900" || !garage.PathStyle {
		t.Fatalf("S3-compatible bucket: %+v, %v", garage, err)
	}
	bucket := "my-videos"
	noRegion := apitypes.VolumeMountSpec{Name: "videos", CloudBucket: &apitypes.CloudBucketSpec{
		Bucket: bucket, AccessKeySecret: &bucket, SecretKeySecret: &bucket,
	}}
	if err := ValidateVolumes([]apitypes.VolumeMountSpec{noRegion}); err == nil {
		t.Fatal("deployed an AWS bucket without a region")
	}
}
