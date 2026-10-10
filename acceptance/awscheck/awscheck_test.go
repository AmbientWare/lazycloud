// Package awscheck checks workspace volumes against real S3 and STS: the
// storage owner creates a workspace bucket and issues a host grant as in
// production, the grant reaches what a volume needs and nothing else, and
// GeeseFS mounts with it over TLS verified against the host's CA bundle,
// as hosts mount. run.sh creates the role and removes everything; it runs
// only with the owner's approval. Without its environment the tests skip.
package awscheck

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	s3types "github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/aws/smithy-go"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/agent"
	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

const mountImage = "alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"

// env is run.sh's: the platform account, the role host grants assume, the
// prefix every bucket of the run starts with, the run's tag and the region.
type env struct {
	account, role, prefix, run, region string
}

func checkEnv(t *testing.T) env {
	t.Helper()
	e := env{
		account: os.Getenv("LAZYCLOUD_AWS_CHECK_ACCOUNT"),
		role:    os.Getenv("LAZYCLOUD_AWS_CHECK_ROLE_ARN"), prefix: os.Getenv("LAZYCLOUD_AWS_CHECK_PREFIX"),
		run: os.Getenv("LAZYCLOUD_AWS_CHECK_RUN"), region: os.Getenv("AWS_REGION"),
	}
	if e.account == "" || e.role == "" || e.prefix == "" || e.run == "" || e.region == "" {
		t.Skip("acceptance/awscheck/run.sh sets up the AWS check")
	}
	return e
}

// platformClient is S3 with the caller's own credentials, as the server
// runs with its own.
func platformClient(t *testing.T, region string) *s3.Client {
	t.Helper()
	cfg, err := config.LoadDefaultConfig(t.Context(), config.WithRegion(region))
	if err != nil {
		t.Fatal(err)
	}
	return s3.NewFromConfig(cfg)
}

// tag marks a bucket with the run, so a cleanup that failed finds it.
func tag(t *testing.T, client *s3.Client, bucket, run string) {
	t.Helper()
	if _, err := client.PutBucketTagging(t.Context(), &s3.PutBucketTaggingInput{
		Bucket: aws.String(bucket), Tagging: &s3types.Tagging{TagSet: []s3types.Tag{
			{Key: aws.String("lazycloud:check"), Value: aws.String(run)},
			{Key: aws.String("lazycloud:expires"), Value: aws.String(time.Now().Add(2 * time.Hour).UTC().Format(time.RFC3339))},
		}},
	}); err != nil {
		t.Fatalf("tag %s: %v", bucket, err)
	}
}

// removeBucket empties and deletes bucket with the platform's credentials.
func removeBucket(client *s3.Client, bucket string) error {
	ctx := context.Background()
	var errs []error
	objects := s3.NewListObjectsV2Paginator(client, &s3.ListObjectsV2Input{Bucket: aws.String(bucket)})
	for objects.HasMorePages() {
		page, err := objects.NextPage(ctx)
		if err != nil {
			return fmt.Errorf("list %s: %w", bucket, err)
		}
		for _, o := range page.Contents {
			if _, err := client.DeleteObject(ctx, &s3.DeleteObjectInput{Bucket: aws.String(bucket), Key: o.Key}); err != nil {
				errs = append(errs, err)
			}
		}
	}
	uploads, err := client.ListMultipartUploads(ctx, &s3.ListMultipartUploadsInput{Bucket: aws.String(bucket)})
	if err == nil {
		for _, u := range uploads.Uploads {
			_, err := client.AbortMultipartUpload(ctx, &s3.AbortMultipartUploadInput{Bucket: aws.String(bucket), Key: u.Key, UploadId: u.UploadId})
			errs = append(errs, err)
		}
	}
	_, err = client.DeleteBucket(ctx, &s3.DeleteBucketInput{Bucket: aws.String(bucket)})
	return errors.Join(append(errs, err)...)
}

func errorCode(err error) string {
	var api smithy.APIError
	if errors.As(err, &api) {
		return api.ErrorCode()
	}
	return ""
}

// A host grant for a fresh workspace reaches the workspace's volume
// objects, finds a missing key missing rather than forbidden, lists and
// aborts multipart uploads, and is refused anything else. GeeseFS mounts
// the volume with it by subdomain over TLS, writes, finds a missing file
// missing, and a second mount reads what the first wrote.
func TestHostGrantMountsAVolume(t *testing.T) {
	e := checkEnv(t)
	ctx := t.Context()
	pool := dbtest.New(t)
	store := storage.NewStorage(pool, storage.Config{
		Region:     e.region,
		Workspaces: storage.WorkspaceBuckets{Provider: storage.ProviderAWS, Prefix: e.prefix, AccountID: e.account, RoleARN: e.role},
	}, nil)
	var ws uuid.UUID
	if err := pool.QueryRow(ctx, "insert into workspaces (name) values ('aws-check') returning id").Scan(&ws); err != nil {
		t.Fatal(err)
	}
	platform := platformClient(t, e.region)
	grant, err := store.HostGrant(ctx, compute.HostID(uuid.New()), identity.WorkspaceID(ws))
	if err != nil {
		t.Fatalf("host grant: %v", err)
	}
	t.Cleanup(func() {
		if err := removeBucket(platform, grant.Bucket); err != nil {
			t.Errorf("remove %s: %v", grant.Bucket, err)
		}
	})
	tag(t, platform, grant.Bucket, e.run)
	if grant.PathStyle || grant.Endpoint != "https://s3."+e.region+".amazonaws.com" {
		t.Fatalf("grant location %+v, want the regional endpoint by subdomain", grant.Location)
	}

	host := s3.New(s3.Options{
		Region: grant.Region, BaseEndpoint: aws.String(grant.Endpoint), UsePathStyle: grant.PathStyle,
		Credentials: credentials.NewStaticCredentialsProvider(grant.AccessKeyID, grant.SecretAccessKey, grant.SessionToken),
	})
	prefix := "volumes/" + uuid.NewString() + "/"
	if _, err := host.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(grant.Bucket), Key: aws.String(prefix + "seed"), Body: strings.NewReader("seed")}); err != nil {
		t.Fatalf("write a volume object: %v", err)
	}
	if _, err := host.HeadObject(ctx, &s3.HeadObjectInput{Bucket: aws.String(grant.Bucket), Key: aws.String(prefix + "missing")}); errorCode(err) != "NotFound" {
		t.Fatalf("head of a missing volume object: %v, want NotFound", err)
	}
	listed, err := host.ListObjectsV2(ctx, &s3.ListObjectsV2Input{Bucket: aws.String(grant.Bucket), Prefix: aws.String(prefix)})
	if err != nil || len(listed.Contents) != 1 {
		t.Fatalf("list the volume: %v %v", listed, err)
	}
	upload, err := host.CreateMultipartUpload(ctx, &s3.CreateMultipartUploadInput{Bucket: aws.String(grant.Bucket), Key: aws.String(prefix + "big")})
	if err != nil {
		t.Fatalf("start a multipart upload: %v", err)
	}
	uploads, err := host.ListMultipartUploads(ctx, &s3.ListMultipartUploadsInput{Bucket: aws.String(grant.Bucket), Prefix: aws.String(prefix)})
	if err != nil || len(uploads.Uploads) != 1 {
		t.Fatalf("list multipart uploads: %v %v", uploads, err)
	}
	if _, err := host.AbortMultipartUpload(ctx, &s3.AbortMultipartUploadInput{Bucket: aws.String(grant.Bucket), Key: aws.String(prefix + "big"), UploadId: upload.UploadId}); err != nil {
		t.Fatalf("abort a multipart upload: %v", err)
	}
	for name, call := range map[string]func() error{
		"write outside volumes/ and disks/": func() error {
			_, err := host.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(grant.Bucket), Key: aws.String("other/x"), Body: strings.NewReader("x")})
			return err
		},
		"list the whole bucket": func() error {
			_, err := host.ListObjectsV2(ctx, &s3.ListObjectsV2Input{Bucket: aws.String(grant.Bucket)})
			return err
		},
		"delete the bucket": func() error {
			_, err := host.DeleteBucket(ctx, &s3.DeleteBucketInput{Bucket: aws.String(grant.Bucket)})
			return err
		},
	} {
		if err := call(); errorCode(err) != "AccessDenied" {
			t.Errorf("%s: %v, want AccessDenied", name, err)
		}
	}

	creds := grantCredentials(t, grant)
	source := grant.Bucket + ":" + prefix
	mount(t, creds, grant.Location, source, `
echo hello >/mnt/v/a.txt
head -c 20000000 /dev/urandom >/mnt/v/big
test ! -e /mnt/v/missing
test "$(cat /mnt/v/seed)" = seed`)
	mount(t, creds, grant.Location, source, `
test "$(cat /mnt/v/a.txt)" = hello
test "$(wc -c </mnt/v/big)" -eq 20000000`)
}

// A user's bucket whose name holds a dot is addressed by path, so TLS
// verifies against the endpoint's own certificate, and GeeseFS mounts it.
func TestDottedCloudBucketMountsByPath(t *testing.T) {
	e := checkEnv(t)
	ctx := t.Context()
	platform := platformClient(t, e.region)
	name := e.prefix + ".dotted"
	input := &s3.CreateBucketInput{Bucket: aws.String(name)}
	if e.region != "us-east-1" {
		input.CreateBucketConfiguration = &s3types.CreateBucketConfiguration{LocationConstraint: s3types.BucketLocationConstraint(e.region)}
	}
	if _, err := platform.CreateBucket(ctx, input); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := removeBucket(platform, name); err != nil {
			t.Errorf("remove %s: %v", name, err)
		}
	})
	tag(t, platform, name, e.run)
	if _, err := platform.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(name), Key: aws.String("data/n"), Body: strings.NewReader("42")}); err != nil {
		t.Fatal(err)
	}
	loc, err := storage.CloudBucketLocation(apitypes.CloudBucketSpec{Bucket: name, Region: &e.region})
	if err != nil || !loc.PathStyle {
		t.Fatalf("location of %s: %+v %v, want path-style", name, loc, err)
	}
	cfg, err := config.LoadDefaultConfig(ctx, config.WithRegion(e.region))
	if err != nil {
		t.Fatal(err)
	}
	keys, err := cfg.Credentials.Retrieve(ctx)
	if err != nil {
		t.Fatal(err)
	}
	creds := writeCredentials(t, keys.AccessKeyID, keys.SecretAccessKey, keys.SessionToken)
	mount(t, creds, loc, name+":data/", `test "$(cat /mnt/v/n)" = 42`)
}

func grantCredentials(t *testing.T, g storage.Grant) string {
	t.Helper()
	return writeCredentials(t, g.AccessKeyID, g.SecretAccessKey, g.SessionToken)
}

// writeCredentials writes keys where GeeseFS reads them, as the agent
// does, and returns the directory.
func writeCredentials(t *testing.T, access, secret, session string) string {
	t.Helper()
	dir := t.TempDir()
	data, err := json.Marshal(map[string]any{"Version": 1, "AccessKeyId": access, "SecretAccessKey": secret, "SessionToken": session})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "credentials.json"), data, 0o644); err != nil { //nolint:gosec // Read by the mount container; removed with the test.
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "config"), []byte("[default]\ncredential_process = cat /creds/credentials.json\n"), 0o644); err != nil { //nolint:gosec // Holds no secret.
		t.Fatal(err)
	}
	return dir
}

// mount runs GeeseFS on source at /mnt/v in a container as hosts' mount
// containers do, with the host's CA bundle, and runs script against it.
func mount(t *testing.T, creds string, loc storage.Location, source, script string) {
	t.Helper()
	geesefs, err := filepath.Abs("../../bin/geesefs")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(geesefs); err != nil {
		t.Fatalf("GeeseFS: %v; run deploy/local/fetch-geesefs.sh", err)
	}
	style := "--subdomain"
	if loc.PathStyle {
		style = ""
	}
	full := fmt.Sprintf(`set -eu
mkdir -p /mnt/v
/opt/geesefs -f --endpoint %q --region %q %s --stat-cache-ttl 1s %q /mnt/v &
pid=$!
for i in $(seq 60); do grep -q ' /mnt/v fuse' /proc/mounts && break; sleep 1; done
grep -q ' /mnt/v fuse' /proc/mounts
%s
sync
kill -TERM $pid
wait $pid || true
`, loc.Endpoint, loc.Region, style, source, script)
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, "docker", "run", "--rm", "--label", "lazycloud.check="+os.Getenv("LAZYCLOUD_AWS_CHECK_RUN"), //nolint:gosec // Fixed image and the test's own paths.
		"--network", "host", "--cap-add", "SYS_ADMIN", "--device", "/dev/fuse", "--security-opt", "apparmor=unconfined",
		"-e", "AWS_SDK_LOAD_CONFIG=1", "-e", "AWS_CONFIG_FILE=/creds/config",
		"-v", geesefs+":/opt/geesefs:ro", "-v", agent.HostTrustBundle()+":/etc/ssl/certs/ca-certificates.crt:ro", "-v", creds+":/creds:ro",
		mountImage, "sh", "-c", full)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("mount %s: %v\n%s", source, err, out)
	}
}
