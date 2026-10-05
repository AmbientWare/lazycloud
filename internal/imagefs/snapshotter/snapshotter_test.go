package snapshotter

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/rand"
	"errors"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	snapshotsapi "github.com/containerd/containerd/api/services/snapshots/v1"
	"github.com/containerd/containerd/v2/core/mount"
	"github.com/containerd/containerd/v2/core/snapshots"
	"github.com/containerd/containerd/v2/core/snapshots/proxy"
	"github.com/containerd/errdefs"
	"github.com/google/uuid"
	"github.com/prometheus/client_golang/prometheus"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// testStore holds layers in the development Garage, read through
// presigned URLs as hosts read the platform store.
type testStore struct {
	client  *s3.Client
	bucket  string
	prefix  string
	presign *s3.PresignClient
}

func newTestStore(t *testing.T) *testStore {
	t.Helper()
	cfg := storagetest.Config()
	client := s3.New(s3.Options{
		Region: cfg.Region, BaseEndpoint: aws.String(cfg.Endpoint), UsePathStyle: true,
		Credentials: credentials.NewStaticCredentialsProvider(cfg.AccessKeyID, cfg.SecretAccessKey, ""),
	})
	ts := &testStore{client: client, bucket: cfg.Bucket, prefix: "snapshotter-test/" + uuid.NewString() + "/", presign: s3.NewPresignClient(client)}
	t.Cleanup(func() {
		ctx := context.Background()
		listed, err := client.ListObjectsV2(ctx, &s3.ListObjectsV2Input{Bucket: aws.String(ts.bucket), Prefix: aws.String(ts.prefix)})
		if err != nil {
			t.Errorf("list test objects: %v", err)
			return
		}
		for _, o := range listed.Contents {
			_, _ = client.DeleteObject(ctx, &s3.DeleteObjectInput{Bucket: aws.String(ts.bucket), Key: o.Key})
		}
	})
	return ts
}

func (ts *testStore) put(t *testing.T, key string, body []byte) {
	t.Helper()
	if _, err := ts.client.PutObject(t.Context(), &s3.PutObjectInput{
		Bucket: aws.String(ts.bucket), Key: aws.String(ts.prefix + key), Body: bytes.NewReader(body),
	}); err != nil {
		t.Fatal(err)
	}
}

func (ts *testStore) remove(t *testing.T, key string) {
	t.Helper()
	if _, err := ts.client.DeleteObject(t.Context(), &s3.DeleteObjectInput{Bucket: aws.String(ts.bucket), Key: aws.String(ts.prefix + key)}); err != nil {
		t.Fatal(err)
	}
}

func (ts *testStore) url(t *testing.T, key string) string {
	t.Helper()
	req, err := ts.presign.PresignGetObject(t.Context(), &s3.GetObjectInput{Bucket: aws.String(ts.bucket), Key: aws.String(ts.prefix + key)},
		s3.WithPresignExpires(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	return req.URL
}

// storedLayer is a converted layer in the test store.
type storedLayer struct {
	index imagefs.Index
	grant layersource.Grant
}

func (ts *testStore) layer(t *testing.T, tarball []byte) storedLayer {
	t.Helper()
	var data bytes.Buffer
	ix, err := imagefs.Convert(t.Context(), bytes.NewReader(tarball), &data)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := ix.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	name := strings.TrimPrefix(string(ix.Layer), "sha256:")
	ts.put(t, name+"/index", raw)
	ts.put(t, name+"/data", data.Bytes())
	return storedLayer{index: ix, grant: layersource.Grant{
		Layer: ix.Layer, IndexURL: ts.url(t, name+"/index"), DataURL: ts.url(t, name+"/data"), ExpiresAt: time.Now().Add(time.Hour),
	}}
}

// countingTransport counts requests and fails those fail picks.
type countingTransport struct {
	requests atomic.Int64
	fail     func(*http.Request) bool
}

func (c *countingTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	c.requests.Add(1)
	if c.fail != nil && c.fail(r) {
		return &http.Response{StatusCode: http.StatusInternalServerError, Body: http.NoBody, Request: r, Header: http.Header{}}, nil
	}
	return http.DefaultTransport.RoundTrip(r)
}

// service is a snapshotter served on a socket as on a host, with
// containerd's proxy client and the agent's layer source client.
type service struct {
	root     string
	sn       snapshots.Snapshotter
	sources  *layersource.Client
	registry *prometheus.Registry
}

func serve(t *testing.T, transport http.RoundTripper) *service {
	t.Helper()
	if _, err := os.Stat("/dev/fuse"); err != nil {
		t.Fatalf("FUSE is unavailable: %v", err)
	}
	root := t.TempDir()
	socket := filepath.Join(root, "snapshotter.sock")
	registry := prometheus.NewRegistry()
	cfg := Config{
		Root: filepath.Join(root, "state"), CacheBytes: 256 << 20, Fetches: 4, AllowOther: os.Geteuid() == 0,
		HTTP: &http.Client{Transport: transport}, Registry: registry, Logger: slog.New(slog.DiscardHandler),
	}
	ctx, cancel := context.WithCancel(context.Background())
	ready := make(chan struct{})
	served := make(chan error, 1)
	go func() { served <- Serve(ctx, cfg, socket, func() { close(ready) }) }()
	select {
	case <-ready:
	case err := <-served:
		cancel()
		t.Fatal(err)
	}
	conn, err := grpc.NewClient("unix:"+socket, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	sources, err := layersource.Dial(socket)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = sources.Close()
		_ = conn.Close()
		cancel()
		if err := <-served; err != nil {
			t.Error(err)
		}
	})
	return &service{root: cfg.Root, sn: proxy.NewSnapshotter(snapshotsapi.NewSnapshotsClient(conn), layersource.Snapshotter), sources: sources, registry: registry}
}

// pull makes a lazy layer present as containerd's unpacker does and
// returns its snapshot name.
func (s *service) pull(t *testing.T, l storedLayer, parent string) string {
	t.Helper()
	key := "extract-" + uuid.NewString()
	_, err := s.sn.Prepare(t.Context(), key, parent, snapshots.WithLabels(map[string]string{
		snapshots.LabelSnapshotRef:    "chain-" + string(l.index.Layer),
		snapshots.LabelSnapshotDiffID: string(l.index.Layer),
		layersource.LazyLabel:         "true",
	}))
	if !errdefs.IsAlreadyExists(err) {
		t.Fatalf("prepare lazy layer: %v, want already exists", err)
	}
	return key
}

// view returns the directory a read-only view of name shows.
func (s *service) view(t *testing.T, key, name string) string {
	t.Helper()
	mounts, err := s.sn.View(t.Context(), key, name)
	if err != nil {
		t.Fatal(err)
	}
	if len(mounts) != 1 || mounts[0].Type != "bind" {
		t.Fatalf("a view of one layer mounts %+v", mounts)
	}
	return mounts[0].Source
}

func layerMounts(t *testing.T, root string) []string {
	t.Helper()
	raw, err := os.ReadFile("/proc/self/mountinfo")
	if err != nil {
		t.Fatal(err)
	}
	var found []string
	for line := range strings.SplitSeq(string(raw), "\n") {
		if fields := strings.Fields(line); len(fields) > 4 && strings.HasPrefix(fields[4], root) && strings.Contains(line, fuseType) {
			found = append(found, fields[4])
		}
	}
	return found
}

type tarEntry struct {
	hdr  tar.Header
	body []byte
}

// buildTar writes a layer whose root the test's user owns, since overlay
// snapshots on top take their owner from it.
func buildTar(t *testing.T, entries []tarEntry) []byte {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	root := tarEntry{hdr: tar.Header{Name: "./", Typeflag: tar.TypeDir, Mode: 0o755, Uid: os.Getuid(), Gid: os.Getgid()}}
	for _, e := range append([]tarEntry{root}, entries...) {
		hdr := e.hdr
		hdr.Size = int64(len(e.body))
		if hdr.ModTime.IsZero() {
			hdr.ModTime = time.Unix(1700000000, 0)
		}
		hdr.Format = tar.FormatPAX
		if err := tw.WriteHeader(&hdr); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write(e.body); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func random(n int) []byte {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return b
}

// A lazily pulled layer is reported present without a download, and its
// mount shows every file of the tar with its bytes and metadata.
func TestLazyLayerServesItsFiles(t *testing.T) {
	ts := newTestStore(t)
	transport := &countingTransport{}
	s := serve(t, transport)
	weights := random(9 << 20)
	tool := random(3000)
	l := ts.layer(t, buildTar(t, []tarEntry{
		{hdr: tar.Header{Name: "etc/", Typeflag: tar.TypeDir, Mode: 0o755}},
		{hdr: tar.Header{Name: "etc/hostname", Typeflag: tar.TypeReg, Mode: 0o644, Uid: 1234, Gid: 5678,
			PAXRecords: map[string]string{"SCHILY.xattr.user.note": "kept"}}, body: []byte("lazy\n")},
		{hdr: tar.Header{Name: "bin/", Typeflag: tar.TypeDir, Mode: 0o755}},
		{hdr: tar.Header{Name: "bin/tool", Typeflag: tar.TypeReg, Mode: 0o4755}, body: tool},
		{hdr: tar.Header{Name: "bin/tool2", Typeflag: tar.TypeLink, Linkname: "bin/tool"}},
		{hdr: tar.Header{Name: "bin/sh", Typeflag: tar.TypeSymlink, Linkname: "tool"}},
		{hdr: tar.Header{Name: "data/weights", Typeflag: tar.TypeReg, Mode: 0o600, Uid: os.Getuid()}, body: weights},
		{hdr: tar.Header{Name: "data/empty", Typeflag: tar.TypeReg, Mode: 0o644}},
		{hdr: tar.Header{Name: "dev/null", Typeflag: tar.TypeChar, Mode: 0o666, Devmajor: 1, Devminor: 3}},
		{hdr: tar.Header{Name: "run/queue", Typeflag: tar.TypeFifo, Mode: 0o644}},
		{hdr: tar.Header{Name: "usr/.wh.removed", Typeflag: tar.TypeReg, Mode: 0o644}},
		{hdr: tar.Header{Name: "opaque/.wh..wh..opq", Typeflag: tar.TypeReg, Mode: 0o644}},
		{hdr: tar.Header{Name: "opaque/kept", Typeflag: tar.TypeReg, Mode: 0o644}, body: []byte("kept")},
	}))
	if err := s.sources.Grant(t.Context(), []layersource.Grant{l.grant}); err != nil {
		t.Fatal(err)
	}
	name := s.pull(t, l, "")
	info, err := s.sn.Stat(t.Context(), name)
	if err != nil || info.Kind != snapshots.KindCommitted {
		t.Fatalf("the lazy layer is %+v, %v; want committed", info, err)
	}
	pulled := transport.requests.Load()
	if pulled != 1 {
		t.Fatalf("the pull made %d store requests, want the index only", pulled)
	}

	dir := s.view(t, "view", name)
	files := map[string]struct {
		body []byte
		mode fs.FileMode
	}{
		"etc/hostname": {[]byte("lazy\n"), 0o644},
		"bin/tool":     {tool, fs.ModeSetuid | 0o755},
		"bin/tool2":    {tool, fs.ModeSetuid | 0o755},
		"data/weights": {weights, 0o600},
		"data/empty":   {nil, 0o644},
		"opaque/kept":  {[]byte("kept"), 0o644},
	}
	for path, want := range files {
		got, err := os.ReadFile(filepath.Join(dir, path))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(got, want.body) {
			t.Fatalf("%s reads %d bytes that differ from the %d in the layer", path, len(got), len(want.body))
		}
		st, err := os.Lstat(filepath.Join(dir, path))
		if err != nil {
			t.Fatal(err)
		}
		if st.Mode() != want.mode || !st.ModTime().Equal(time.Unix(1700000000, 0)) {
			t.Fatalf("%s has mode %v and time %v", path, st.Mode(), st.ModTime())
		}
	}
	var hostname, tool1, tool2 unix.Stat_t
	for p, st := range map[string]*unix.Stat_t{"etc/hostname": &hostname, "bin/tool": &tool1, "bin/tool2": &tool2} {
		if err := unix.Lstat(filepath.Join(dir, p), st); err != nil {
			t.Fatal(err)
		}
	}
	if hostname.Uid != 1234 || hostname.Gid != 5678 {
		t.Fatalf("etc/hostname is owned by %d:%d", hostname.Uid, hostname.Gid)
	}
	if tool1.Ino != tool2.Ino || tool1.Nlink != 2 {
		t.Fatalf("hard links have inodes %d and %d, %d links", tool1.Ino, tool2.Ino, tool1.Nlink)
	}
	note := make([]byte, 16)
	if n, err := unix.Lgetxattr(filepath.Join(dir, "etc/hostname"), "user.note", note); err != nil || string(note[:n]) != "kept" {
		t.Fatalf("user.note is %q, %v", note[:n], err)
	}
	if target, err := os.Readlink(filepath.Join(dir, "bin/sh")); err != nil || target != "tool" {
		t.Fatalf("bin/sh links to %q, %v", target, err)
	}
	var dev, whiteout, fifo unix.Stat_t
	for p, st := range map[string]*unix.Stat_t{"dev/null": &dev, "usr/removed": &whiteout, "run/queue": &fifo} {
		if err := unix.Lstat(filepath.Join(dir, p), st); err != nil {
			t.Fatal(err)
		}
	}
	if dev.Mode&unix.S_IFMT != unix.S_IFCHR || unix.Major(dev.Rdev) != 1 || unix.Minor(dev.Rdev) != 3 {
		t.Fatalf("dev/null is mode %o rdev %d:%d", dev.Mode, unix.Major(dev.Rdev), unix.Minor(dev.Rdev))
	}
	if whiteout.Mode&unix.S_IFMT != unix.S_IFCHR || whiteout.Rdev != 0 {
		t.Fatalf("the whiteout is mode %o rdev %d, want an overlayfs whiteout", whiteout.Mode, whiteout.Rdev)
	}
	if fifo.Mode&unix.S_IFMT != unix.S_IFIFO {
		t.Fatalf("run/queue is mode %o", fifo.Mode)
	}
	if fetched := transport.requests.Load() - pulled; fetched != int64(len(l.index.Frames)) {
		t.Fatalf("reading every file made %d store requests for %d frames", fetched, len(l.index.Frames))
	}
}

// The overlayfs opaque marker is the trusted xattr the kernel reads from a
// lower layer; only root reads trusted xattrs, so the node answers here.
func TestOpaqueDirectoriesCarryTheOverlayMarker(t *testing.T) {
	n := &node{entry: &imagefs.Entry{Path: "opaque", Type: imagefs.TypeDirectory, Opaque: true, Xattrs: map[string][]byte{"user.a": []byte("b")}}}
	value := make([]byte, 4)
	if size, errno := n.Getxattr(t.Context(), opaqueXattr, value); errno != 0 || string(value[:size]) != "y" {
		t.Fatalf("%s is %q, %v", opaqueXattr, value[:size], errno)
	}
	list := make([]byte, 64)
	size, errno := n.Listxattr(t.Context(), list)
	if errno != 0 || string(list[:size]) != opaqueXattr+"\x00user.a\x00" {
		t.Fatalf("xattrs listed %q, %v", list[:size], errno)
	}
	plain := &node{entry: &imagefs.Entry{Path: "plain", Type: imagefs.TypeDirectory}}
	if _, errno := plain.Getxattr(t.Context(), opaqueXattr, value); errno != syscall.ENODATA {
		t.Fatalf("a plain directory answers %v", errno)
	}
}

// Without a live grant a layer is not made present, and the error says so.
func TestPullingAnUngrantedLayerFails(t *testing.T) {
	ts := newTestStore(t)
	s := serve(t, http.DefaultTransport)
	l := ts.layer(t, buildTar(t, []tarEntry{{hdr: tar.Header{Name: "a", Typeflag: tar.TypeReg, Mode: 0o644}, body: []byte("a")}}))
	_, err := s.sn.Prepare(t.Context(), "extract", "", snapshots.WithLabels(map[string]string{
		snapshots.LabelSnapshotRef: "chain", snapshots.LabelSnapshotDiffID: string(l.index.Layer), layersource.LazyLabel: "true",
	}))
	if !errdefs.IsFailedPrecondition(err) || !strings.Contains(err.Error(), "no live grant") {
		t.Fatalf("prepare without a grant: %v", err)
	}
	expired := l.grant
	expired.ExpiresAt = time.Now().Add(-time.Second)
	if err := s.sources.Grant(t.Context(), []layersource.Grant{expired}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.sn.Prepare(t.Context(), "extract", "", snapshots.WithLabels(map[string]string{
		snapshots.LabelSnapshotRef: "chain", snapshots.LabelSnapshotDiffID: string(l.index.Layer), layersource.LazyLabel: "true",
	})); !errdefs.IsFailedPrecondition(err) {
		t.Fatalf("prepare with an expired grant: %v", err)
	}
}

// A read the store cannot serve reaches the container as EIO, counted,
// after a bounded number of attempts; it never reads as wrong or empty
// bytes.
func TestUnservableReadsAreIOErrors(t *testing.T) {
	ts := newTestStore(t)
	var failing atomic.Bool
	transport := &countingTransport{fail: func(*http.Request) bool { return failing.Load() }}
	s := serve(t, transport)
	l := ts.layer(t, buildTar(t, []tarEntry{
		{hdr: tar.Header{Name: "a", Typeflag: tar.TypeReg, Mode: 0o644}, body: random(1 << 20)},
		{hdr: tar.Header{Name: "b", Typeflag: tar.TypeReg, Mode: 0o644}, body: random(1 << 20)},
	}))
	if err := s.sources.Grant(t.Context(), []layersource.Grant{l.grant}); err != nil {
		t.Fatal(err)
	}
	dir := s.view(t, "view", s.pull(t, l, ""))

	failing.Store(true)
	before := transport.requests.Load()
	if _, err := os.ReadFile(filepath.Join(dir, "a")); !errors.Is(err, syscall.EIO) {
		t.Fatalf("a read the store refuses returns %v", err)
	}
	// The kernel may retry a failed read once itself; each read the
	// snapshotter answers makes exactly fetchAttempts tries.
	if attempts := transport.requests.Load() - before; attempts == 0 || attempts%fetchAttempts != 0 || attempts > 2*fetchAttempts {
		t.Fatalf("the read tried %d times, want %d per kernel read", attempts, fetchAttempts)
	}
	failing.Store(false)
	ts.remove(t, strings.TrimPrefix(string(l.index.Layer), "sha256:")+"/data")
	if _, err := os.ReadFile(filepath.Join(dir, "b")); !errors.Is(err, syscall.EIO) {
		t.Fatalf("a read of a deleted object returns %v", err)
	}
	if got := counterValue(t, s.registry, "lazycloud_snapshotter_read_failures_total"); got < 2 {
		t.Fatalf("%v read failures counted, want at least 2", got)
	}
}

func counterValue(t *testing.T, registry *prometheus.Registry, name string) float64 {
	t.Helper()
	families, err := registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range families {
		if f.GetName() == name {
			return f.GetMetric()[0].GetCounter().GetValue()
		}
	}
	t.Fatalf("no metric %s", name)
	return 0
}

// A layer stays mounted while a view or container uses it and is unmounted
// once none does; a busy mount is kept and retried.
func TestUnusedLayersAreUnmounted(t *testing.T) {
	ts := newTestStore(t)
	s := serve(t, http.DefaultTransport)
	l := ts.layer(t, buildTar(t, []tarEntry{{hdr: tar.Header{Name: "a", Typeflag: tar.TypeReg, Mode: 0o644}, body: []byte("a")}}))
	if err := s.sources.Grant(t.Context(), []layersource.Grant{l.grant}); err != nil {
		t.Fatal(err)
	}
	name := s.pull(t, l, "")
	if got := layerMounts(t, s.root); len(got) != 0 {
		t.Fatalf("a pulled layer no snapshot uses is mounted at %v", got)
	}
	dir := s.view(t, "view", name)
	if got := layerMounts(t, s.root); len(got) != 1 || got[0] != dir {
		t.Fatalf("a viewed layer is mounted at %v, want %s", got, dir)
	}
	open, err := os.Open(filepath.Join(dir, "a"))
	if err != nil {
		t.Fatal(err)
	}
	if err := s.sn.Remove(t.Context(), "view"); err != nil {
		t.Fatal(err)
	}
	if got := layerMounts(t, s.root); len(got) != 1 {
		t.Fatalf("a layer with an open file is mounted at %v, want kept", got)
	}
	if err := s.sn.Remove(t.Context(), name); !errdefs.IsFailedPrecondition(err) {
		t.Fatalf("removing a busy layer: %v", err)
	}
	_ = open.Close()
	if err := s.sn.(snapshots.Cleaner).Cleanup(t.Context()); err != nil { //nolint:forcetypeassert // the proxy cleans up
		t.Fatal(err)
	}
	if got := layerMounts(t, s.root); len(got) != 0 {
		t.Fatalf("an unused layer is still mounted at %v", got)
	}
	if err := s.sn.Remove(t.Context(), name); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Dir(dir)); !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("the removed layer's directory: %v", err)
	}
}

// A container's overlay names its lazy layers' mounts, lowest last.
func TestContainersStackLazyLayers(t *testing.T) {
	ts := newTestStore(t)
	s := serve(t, http.DefaultTransport)
	base := ts.layer(t, buildTar(t, []tarEntry{{hdr: tar.Header{Name: "base", Typeflag: tar.TypeReg, Mode: 0o644}, body: []byte("base")}}))
	top := ts.layer(t, buildTar(t, []tarEntry{{hdr: tar.Header{Name: "top", Typeflag: tar.TypeReg, Mode: 0o644}, body: []byte("top")}}))
	if err := s.sources.Grant(t.Context(), []layersource.Grant{base.grant, top.grant}); err != nil {
		t.Fatal(err)
	}
	lower := s.pull(t, base, "")
	upper := s.pull(t, top, lower)
	mounts, err := s.sn.Prepare(t.Context(), "container", upper)
	if err != nil {
		t.Fatal(err)
	}
	got := layerMounts(t, s.root)
	if len(mounts) != 1 || mounts[0].Type != "overlay" || len(got) != 2 {
		t.Fatalf("a container over two lazy layers mounts %+v with layers at %v", mounts, got)
	}
	lowers := lowerDirs(mounts[0])
	if len(lowers) != 2 {
		t.Fatalf("overlay lower dirs %v", lowers)
	}
	for i, file := range []string{"top", "base"} {
		if _, err := os.Stat(filepath.Join(lowers[i], file)); err != nil {
			t.Fatalf("lower dir %d: %v", i, err)
		}
	}
}

func lowerDirs(m mount.Mount) []string {
	for _, o := range m.Options {
		if v, ok := strings.CutPrefix(o, "lowerdir="); ok {
			return strings.Split(v, ":")
		}
	}
	return nil
}

// Concurrent reads of one frame fetch it from the store once.
func TestConcurrentReadsFetchAFrameOnce(t *testing.T) {
	ts := newTestStore(t)
	transport := &countingTransport{}
	l := ts.layer(t, buildTar(t, []tarEntry{{hdr: tar.Header{Name: "a", Typeflag: tar.TypeReg, Mode: 0o644}, body: random(6 << 20)}}))
	g := newGrants(time.Now)
	g.put(map[imagefs.Digest]grant{l.index.Layer: {indexURL: l.grant.IndexURL, dataURL: l.grant.DataURL, expires: l.grant.ExpiresAt}})
	m, err := newMetrics(prometheus.NewRegistry())
	if err != nil {
		t.Fatal(err)
	}
	frames, err := newFrameCache(t.TempDir(), 64<<20, 4, g, m, slog.New(slog.DiscardHandler))
	if err != nil {
		t.Fatal(err)
	}
	ly := &layer{digest: l.index.Layer, index: l.index, frames: frames,
		data: imagefs.HTTPObject(&http.Client{Transport: transport}, func() string { return l.grant.DataURL })}
	var wg sync.WaitGroup
	errs := make(chan error, 32)
	for range 32 {
		wg.Go(func() {
			p := make([]byte, 4096)
			errs <- frames.read(t.Context(), ly, 1, p, 100)
		})
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if n := transport.requests.Load(); n != 1 {
		t.Fatalf("32 concurrent reads of one frame made %d requests", n)
	}
}

// The cache keeps under its bound while two layers alternate, evicting the
// unmounted layer's frames before the mounted one's.
func TestFrameCacheStaysUnderItsBound(t *testing.T) {
	ts := newTestStore(t)
	g := newGrants(time.Now)
	m, err := newMetrics(prometheus.NewRegistry())
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	const bound = 64 << 20
	frames, err := newFrameCache(dir, bound, 4, g, m, slog.New(slog.DiscardHandler))
	if err != nil {
		t.Fatal(err)
	}
	layerOf := func(size int) *layer {
		l := ts.layer(t, buildTar(t, []tarEntry{{hdr: tar.Header{Name: "a", Typeflag: tar.TypeReg, Mode: 0o644}, body: random(size)}}))
		g.put(map[imagefs.Digest]grant{l.index.Layer: {indexURL: l.grant.IndexURL, dataURL: l.grant.DataURL, expires: l.grant.ExpiresAt}})
		url := l.grant.DataURL
		return &layer{digest: l.index.Layer, index: l.index, frames: frames, data: imagefs.HTTPObject(http.DefaultClient, func() string { return url })}
	}
	mounted := layerOf(8 * imagefs.FrameSize)
	other := layerOf(24 * imagefs.FrameSize)
	frames.setMounted(mounted.digest, 1)
	readAll := func(l *layer) {
		for i := range l.index.Frames {
			if err := frames.read(t.Context(), l, i, make([]byte, 1), 0); err != nil {
				t.Fatal(err)
			}
			if onDisk := diskBytes(t, dir); onDisk > bound {
				t.Fatalf("the cache holds %d bytes on disk, bound %d", onDisk, bound)
			}
		}
	}
	for range 2 {
		readAll(mounted)
		readAll(other)
	}
	for i := range mounted.index.Frames {
		if !frames.touch(frameKey{layer: mounted.digest, frame: i}) {
			t.Fatalf("frame %d of the mounted layer was evicted before the unmounted layer's", i)
		}
	}
	if frames.used > bound {
		t.Fatalf("the cache counts %d bytes, bound %d", frames.used, bound)
	}
}

func diskBytes(t *testing.T, dir string) int64 {
	t.Helper()
	var total int64
	err := filepath.WalkDir(dir, func(_ string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		total += info.Size()
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return total
}

// The grant expiring last wins whatever order grants arrive in, and a call
// with a bad grant records none of its grants.
func TestGrantsKeepTheLatestExpiry(t *testing.T) {
	ts := newTestStore(t)
	s := serve(t, http.DefaultTransport)
	tarball := func(name string) []byte {
		return buildTar(t, []tarEntry{{hdr: tar.Header{Name: name, Typeflag: tar.TypeReg, Mode: 0o644}, body: []byte(name)}})
	}
	l := ts.layer(t, tarball("a"))
	other := ts.layer(t, tarball("b"))
	sooner := layersource.Grant{
		Layer: l.index.Layer, IndexURL: "http://127.0.0.1:1/index", DataURL: "http://127.0.0.1:1/data", ExpiresAt: time.Now().Add(time.Minute),
	}
	if err := s.sources.Grant(t.Context(), []layersource.Grant{l.grant}); err != nil {
		t.Fatal(err)
	}
	if err := s.sources.Grant(t.Context(), []layersource.Grant{sooner}); err != nil {
		t.Fatal(err)
	}
	s.pull(t, l, "")

	bad := other.grant
	bad.Layer = "sha256:short"
	if err := s.sources.Grant(t.Context(), []layersource.Grant{other.grant, bad}); status.Code(errors.Unwrap(err)) != codes.InvalidArgument {
		t.Fatalf("a call with a bad digest: %v", err)
	}
	_, err := s.sn.Prepare(t.Context(), "extract", "", snapshots.WithLabels(map[string]string{
		snapshots.LabelSnapshotRef: "chain", snapshots.LabelSnapshotDiffID: string(other.index.Layer), layersource.LazyLabel: "true",
	}))
	if !errdefs.IsFailedPrecondition(err) {
		t.Fatalf("a refused call recorded its grants: %v", err)
	}
}
