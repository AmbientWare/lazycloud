package acceptance

import (
	"fmt"
	"net/http"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

const bucketApp = `
from pathlib import Path


def read() -> int:
    total = int(Path("/hosted/n").read_text()) + int(Path("/pathed/n").read_text())
    try:
        Path("/hosted/written").write_text("x")
    except OSError:
        return total
    return -1
`

// A cloud bucket mounts over HTTPS from a store whose CA only the host's
// trust bundle holds, named in the host name or in the path, and a
// read-only mount refuses writes.
func TestCloudBucketsMountOverHTTPS(t *testing.T) {
	p := startPlatform(t)
	if p.geesefs == "" {
		t.Skip("volume mounts need GeeseFS; run deploy/local/fetch-geesefs.sh")
	}
	ctx := t.Context()
	prefix := "cloud/" + uuid.NewString() + "/"
	if _, err := tlsStore.Client(true).PutObject(ctx, &s3.PutObjectInput{
		Bucket: aws.String(objectStore.Bucket), Key: aws.String(prefix + "n"), Body: strings.NewReader("20"),
	}); err != nil {
		t.Fatal(err)
	}
	for name, value := range map[string]string{"STORE_KEY": objectStore.AccessKeyID, "STORE_SECRET": objectStore.SecretAccessKey} {
		if _, err := p.secrets.Create(ctx, p.workspace.ID, name, value); err != nil {
			t.Fatal(err)
		}
	}
	bucket := func(name, mountPath string, pathStyle bool) apitypes.VolumeMountSpec {
		readOnly := true
		return apitypes.VolumeMountSpec{Name: name, MountPath: &mountPath, ReadOnly: &readOnly, CloudBucket: &apitypes.CloudBucketSpec{
			Bucket: objectStore.Bucket, Prefix: &prefix, Region: &objectStore.Region, Endpoint: &tlsStore.Endpoint,
			ForcePathStyle: &pathStyle, AccessKeySecret: "STORE_KEY", SecretKeySecret: "STORE_SECRET",
		}}
	}
	s := endpointSpec(p.upload(map[string]string{"app.py": bucketApp}), "read", "app:read", "/")
	s.Volumes = &[]apitypes.VolumeMountSpec{bucket("hosted", "/hosted", false), bucket("pathed", "/pathed", true)}
	p.deploy("buckets", s)
	w := p.describe("buckets", apitypes.WorkloadKindEndpoint, "read")
	if status, _, body := p.call(http.MethodGet, w.Url, ""); status != http.StatusOK || body != fmt.Sprint(40) {
		t.Fatalf("read both mounts: %d %s", status, body)
	}
}

func ptr[T any](v T) *T { return &v }
