package storage_test

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"net/http"
	"slices"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	. "github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

const volumeSpec = `{"name":"fn","volumes":[{"name":"data"},{"name":"bucket","cloud_bucket":{"bucket":"elsewhere"}}]}`

func putFile(t *testing.T, s *Storage, f *fixture, path string, body []byte) {
	t.Helper()
	url, err := s.PresignVolumeFile(t.Context(), f.ws, "data", apitypes.PresignVolumeFileRequest{Path: path, Method: apitypes.PresignVolumeFileRequestMethodPut})
	if err != nil {
		t.Fatal(err)
	}
	if status, _ := send(t, url.Url, body); status != http.StatusOK {
		t.Fatalf("put %s: status %d", path, status)
	}
}

func paths(files []apitypes.VolumeFile) []string {
	out := make([]string, len(files))
	for n, file := range files {
		out[n] = file.Path
		if file.IsDir {
			out[n] += "/"
		}
	}
	return out
}

func TestVolumeFiles(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, volumeSpec)
	s := f.storage
	if _, err := s.CreateVolume(ctx, f.ws, "data"); err != nil {
		t.Fatal(err)
	}
	putFile(t, s, f, "a.txt", []byte("alpha"))
	putFile(t, s, f, "dir/b.txt", []byte("beta"))
	putFile(t, s, f, "dir/sub/c.txt", []byte("gamma"))

	root, err := s.ListVolumeFiles(ctx, f.ws, "data", "", "", 1000)
	if err != nil || !slices.Equal(paths(root.Files), []string{"dir/", "a.txt"}) {
		t.Fatalf("root listing: %v err=%v", paths(root.Files), err)
	}
	dir, err := s.ListVolumeFiles(ctx, f.ws, "data", "./dir/", "", 1000)
	if err != nil || !slices.Equal(paths(dir.Files), []string{"dir/sub/", "dir/b.txt"}) {
		t.Fatalf("dir listing: %v err=%v", paths(dir.Files), err)
	}
	if stat, err := s.StatVolumeFile(ctx, f.ws, "data", "dir"); err != nil || !stat.IsDir {
		t.Fatalf("stat dir: %+v err=%v", stat, err)
	}
	if stat, err := s.StatVolumeFile(ctx, f.ws, "data", "a.txt"); err != nil || stat.SizeBytes != 5 || stat.ModifiedAt == nil {
		t.Fatalf("stat file: %+v err=%v", stat, err)
	}
	if _, err := s.StatVolumeFile(ctx, f.ws, "data", "missing"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("stat missing: %v", err)
	}
	var bad *InvalidError
	for _, p := range []string{"../x", "/etc/passwd", "dir/../../x"} {
		if _, err := s.StatVolumeFile(ctx, f.ws, "data", p); !errors.As(err, &bad) {
			t.Fatalf("stat %q: %v, want invalid", p, err)
		}
	}

	moved, err := s.MoveVolumeFile(ctx, f.ws, "data", "dir", "renamed")
	if err != nil || !moved.IsDir || moved.Path != "renamed" {
		t.Fatalf("move dir: %+v err=%v", moved, err)
	}
	var exists *ConflictError
	if _, err := s.MoveVolumeFile(ctx, f.ws, "data", "a.txt", "renamed/b.txt"); !errors.As(err, &exists) {
		t.Fatalf("move onto an existing file: %v", err)
	}
	if _, err := s.MoveVolumeFile(ctx, f.ws, "data", "renamed", "renamed/inner"); !errors.As(err, &bad) {
		t.Fatalf("move into itself: %v", err)
	}
	url, err := s.PresignVolumeFile(ctx, f.ws, "data", apitypes.PresignVolumeFileRequest{Path: "renamed/sub/c.txt", Method: apitypes.PresignVolumeFileRequestMethodGet})
	if err != nil {
		t.Fatal(err)
	}
	if status, body, _ := get(t, url.Url); status != http.StatusOK || string(body) != "gamma" {
		t.Fatalf("read moved file: %d %q", status, body)
	}

	removed, err := s.RemoveVolumeFiles(ctx, f.ws, "data", "renamed")
	slices.Sort(removed)
	if err != nil || !slices.Equal(removed, []string{"renamed/b.txt", "renamed/sub/c.txt"}) {
		t.Fatalf("remove dir: %v err=%v", removed, err)
	}
	if _, err := s.RemoveVolumeFiles(ctx, f.ws, "data", "."); !errors.As(err, &bad) {
		t.Fatalf("remove of the root: %v", err)
	}
}

// TestBrowserUploadsFromTheDashboard: a workspace bucket answers the
// dashboard's CORS preflight for a presigned PUT and refuses other origins,
// so uploads go from the page straight to the store, and a download URL
// makes the browser save the file.
func TestBrowserUploadsFromTheDashboard(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, volumeSpec)
	var s *Storage
	cfg := withLinks(t, func() *Storage { return s })
	cfg.BrowserOrigin = "https://dashboard.test/"
	s = NewStorage(f.pool, cfg)
	if _, err := s.CreateVolume(ctx, f.ws, "data"); err != nil {
		t.Fatal(err)
	}
	target, err := s.PresignVolumeFile(ctx, f.ws, "data", apitypes.PresignVolumeFileRequest{Path: "a.txt", Method: apitypes.PresignVolumeFileRequestMethodPut})
	if err != nil {
		t.Fatal(err)
	}
	// preflight returns the status and allowed origin of a browser's CORS
	// preflight for the presigned PUT.
	preflight := func(origin string) (int, string) {
		req, err := http.NewRequestWithContext(ctx, http.MethodOptions, target.Url, nil)
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Origin", origin)
		req.Header.Set("Access-Control-Request-Method", http.MethodPut)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = resp.Body.Close() }()
		return resp.StatusCode, resp.Header.Get("Access-Control-Allow-Origin")
	}
	if status, allowed := preflight("https://dashboard.test"); status != http.StatusOK || allowed != "https://dashboard.test" {
		t.Fatalf("dashboard preflight: %d %q", status, allowed)
	}
	if status, _ := preflight("https://elsewhere.test"); status != http.StatusForbidden {
		t.Fatalf("another origin's preflight: %d", status)
	}

	// A download link makes the browser save the file rather than show it.
	f.storage = s
	putFile(t, s, f, "dir/notes.txt", []byte("notes"))
	download := true
	saved, err := s.PresignVolumeFile(ctx, f.ws, "data", apitypes.PresignVolumeFileRequest{
		Path: "dir/notes.txt", Method: apitypes.PresignVolumeFileRequestMethodGet, Download: &download,
	})
	if err != nil {
		t.Fatal(err)
	}
	if status, body, header := get(t, saved.Url); status != http.StatusOK || string(body) != "notes" ||
		header.Get("Content-Disposition") != `attachment; filename=notes.txt` {
		t.Fatalf("download: %d %q %q", status, body, header.Get("Content-Disposition"))
	}
}

// TestMoveKeepsNamesThatLookEscaped: copy sources are URL paths, so a file
// named "a%20b" must not copy "a b".
func TestMoveKeepsNamesThatLookEscaped(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, volumeSpec)
	s := f.storage
	if _, err := s.CreateVolume(ctx, f.ws, "data"); err != nil {
		t.Fatal(err)
	}
	putFile(t, s, f, "src/a%20b.txt", []byte("percent"))
	putFile(t, s, f, "src/a b.txt", []byte("space"))
	if _, err := s.MoveVolumeFile(ctx, f.ws, "data", "src", "dst"); err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]string{"dst/a%20b.txt": "percent", "dst/a b.txt": "space"} {
		url, err := s.PresignVolumeFile(ctx, f.ws, "data", apitypes.PresignVolumeFileRequest{Path: name, Method: apitypes.PresignVolumeFileRequestMethodGet})
		if err != nil {
			t.Fatal(err)
		}
		if status, body, _ := get(t, url.Url); status != http.StatusOK || string(body) != want {
			t.Fatalf("%s after move: %d %q, want %q", name, status, body, want)
		}
	}
}

func TestVolumeMultipartUpload(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, volumeSpec)
	s := f.storage
	if _, err := s.CreateVolume(ctx, f.ws, "data"); err != nil {
		t.Fatal(err)
	}
	body := bytes.Repeat([]byte("0123456789abcdef"), (5<<20)/16+100)
	partSize := int64(5 << 20)
	upload, err := s.CreateVolumeUpload(ctx, f.ws, "data", apitypes.CreateVolumeUploadRequest{Path: "big.bin", SizeBytes: int64(len(body)), PartSizeBytes: &partSize})
	if err != nil || len(upload.Parts) != 2 {
		t.Fatalf("create upload: %d parts, err=%v", len(upload.Parts), err)
	}
	var parts []apitypes.CompletedPart
	for _, p := range upload.Parts {
		status, etag := send(t, p.Url, body[p.Offset:p.Offset+p.SizeBytes])
		if status != http.StatusOK || etag == "" {
			t.Fatalf("part %d: status %d etag %q", p.Number, status, etag)
		}
		parts = append(parts, apitypes.CompletedPart{Number: p.Number, Etag: etag})
	}
	file, err := s.CompleteVolumeUpload(ctx, f.ws, "data", apitypes.CompleteVolumeUploadRequest{Path: "big.bin", UploadId: upload.UploadId, Parts: parts})
	if err != nil || file.SizeBytes != int64(len(body)) {
		t.Fatalf("complete: %+v err=%v", file, err)
	}

	aborted, err := s.CreateVolumeUpload(ctx, f.ws, "data", apitypes.CreateVolumeUploadRequest{Path: "gone.bin", SizeBytes: 10})
	if err != nil {
		t.Fatal(err)
	}
	if err := s.AbortVolumeUpload(ctx, f.ws, "data", apitypes.AbortVolumeUploadRequest{Path: "gone.bin", UploadId: aborted.UploadId}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.CompleteVolumeUpload(ctx, f.ws, "data", apitypes.CompleteVolumeUploadRequest{
		Path: "gone.bin", UploadId: aborted.UploadId, Parts: []apitypes.CompletedPart{{Number: 1, Etag: "x"}},
	}); !errors.Is(err, ErrNotFound) {
		t.Fatalf("complete after abort: %v", err)
	}
}

// TestVolumeDeletionChecksLiveMounts covers the transactional invariant: a
// volume a running container mounts cannot be deleted, and a deleted
// volume's name is free at once for a new, empty volume.
func TestVolumeDeletionChecksLiveMounts(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, volumeSpec)
	s := f.storage
	container := f.container()
	specs := []apitypes.VolumeMountSpec{
		{Name: "data"},
		{Name: "bucket", CloudBucket: &apitypes.CloudBucketSpec{Bucket: "elsewhere"}},
	}
	mounts, err := s.MountVolumes(ctx, f.ws, container, specs)
	if err != nil {
		t.Fatal(err)
	}
	if mounts[0].Volume == nil || mounts[0].MountPath != "/volumes/data" || mounts[1].CloudBucket == nil || mounts[1].Volume != nil {
		t.Fatalf("mounts: %+v", mounts)
	}
	again, err := s.MountVolumes(ctx, f.ws, container, specs)
	if err != nil || *again[0].Volume != *mounts[0].Volume {
		t.Fatalf("remount: %+v err=%v", again, err)
	}
	// A cloud bucket is not a platform volume.
	if _, err := s.GetVolume(ctx, f.ws, "bucket"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("cloud bucket as volume: %v", err)
	}
	volume, err := s.GetVolume(ctx, f.ws, "data")
	if err != nil || len(volume.UsedBy) != 1 || volume.UsedBy[0].Name != "fn" {
		t.Fatalf("used by: %+v err=%v", volume, err)
	}
	putFile(t, s, f, "keep.txt", []byte("x"))

	var inUse *ConflictError
	if err := s.DeleteVolume(ctx, f.ws, "data"); !errors.As(err, &inUse) {
		t.Fatalf("delete while mounted: %v", err)
	}
	f.stop(container, "stopped")
	if err := s.DeleteVolume(ctx, f.ws, "data"); err != nil {
		t.Fatal(err)
	}
	fresh, err := s.CreateVolume(ctx, f.ws, "data")
	if err != nil || fresh.Id == volume.Id {
		t.Fatalf("recreated volume: %+v err=%v", fresh, err)
	}
	if listing, err := s.ListVolumeFiles(ctx, f.ws, "data", "", "", 100); err != nil || len(listing.Files) != 0 {
		t.Fatalf("recreated volume files: %v err=%v", paths(listing.Files), err)
	}

	if _, err := s.Sweep(ctx, slog.Default()); err != nil {
		t.Fatal(err)
	}
	var left int
	if err := f.pool.QueryRow(ctx, `select count(*) from volumes where id = $1`, volume.Id).Scan(&left); err != nil || left != 0 {
		t.Fatalf("deleted volume rows after sweep: %d err=%v", left, err)
	}
	client := storagetest.Client()
	bucket := bucketOf(t, f)
	out, err := client.ListObjectsV2(ctx, &s3.ListObjectsV2Input{Bucket: aws.String(bucket), Prefix: aws.String("volumes/" + volume.Id.String() + "/")})
	if err != nil || len(out.Contents) != 0 {
		t.Fatalf("deleted volume objects after sweep: %d err=%v", len(out.Contents), err)
	}
}

func bucketOf(t *testing.T, f *fixture) string {
	t.Helper()
	var bucket string
	if err := f.pool.QueryRow(t.Context(), `select bucket from workspace_buckets where workspace_id = $1`, uuid.UUID(f.ws)).Scan(&bucket); err != nil {
		t.Fatal(err)
	}
	return bucket
}

// TestHostGrantReachesOneWorkspace proves a host credential reaches its
// workspace bucket and nothing else, and that expired keys are revoked.
func TestHostGrantReachesOneWorkspace(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, volumeSpec)
	s := f.storage
	other := f.workspace("other-" + uuid.NewString()[:8])
	if _, err := s.HostGrant(ctx, f.host, other); err != nil {
		t.Fatal(err)
	}
	grant, err := s.HostGrant(ctx, f.host, f.ws)
	if err != nil {
		t.Fatal(err)
	}
	client := s3.New(s3.Options{
		Region: grant.Region, BaseEndpoint: aws.String(grant.Endpoint), UsePathStyle: true,
		Credentials: credentials.NewStaticCredentialsProvider(grant.AccessKeyID, grant.SecretAccessKey, grant.SessionToken),
	})
	write := func(bucket string) error {
		_, err := client.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(bucket), Key: aws.String("probe"), Body: strings.NewReader("x")})
		return err
	}
	if err := write(grant.Bucket); err != nil {
		t.Fatalf("write own workspace bucket: %v", err)
	}
	var otherBucket string
	if err := f.pool.QueryRow(ctx, `select bucket from workspace_buckets where workspace_id = $1`, uuid.UUID(other)).Scan(&otherBucket); err != nil {
		t.Fatal(err)
	}
	if err := write(otherBucket); err == nil {
		t.Fatal("the grant wrote another workspace's bucket")
	}
	if err := write(f.bucket); err == nil {
		t.Fatal("the grant wrote the platform bucket")
	}

	if _, err := f.pool.Exec(ctx, `update storage_grants set expires_at = now() - interval '1 second' where access_key_id = $1`, grant.AccessKeyID); err != nil {
		t.Fatal(err)
	}
	result, err := s.Sweep(context.WithoutCancel(ctx), slog.Default())
	if err != nil || result.Grants < 1 {
		t.Fatalf("sweep revoked %d grants, err=%v", result.Grants, err)
	}
	if err := write(grant.Bucket); err == nil {
		t.Fatal("a revoked grant still writes")
	}
}
