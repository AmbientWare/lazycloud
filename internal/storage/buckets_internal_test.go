package storage

import (
	"encoding/json"
	"strings"
	"testing"
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
