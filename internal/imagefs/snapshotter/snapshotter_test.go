//go:build hostruntime

package snapshotter

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"io/fs"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	snapshotsapi "github.com/containerd/containerd/api/services/snapshots/v1"
	"github.com/containerd/containerd/v2/core/mount"
	"github.com/containerd/containerd/v2/core/snapshots"
	"github.com/containerd/containerd/v2/core/snapshots/proxy"
	"github.com/containerd/errdefs"
	"github.com/google/uuid"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

// service is a snapshotter served on a socket as on a host, with
// containerd's proxy client and the agent's layer source client.
type service struct {
	root     string
	sn       snapshots.Snapshotter
	sources  *layersource.Client
	failures *failedReads
	stop     func()
}

// failedReads counts the container reads the snapshotter logs as failed.
type failedReads struct{ n atomic.Int64 }

func (f *failedReads) Enabled(context.Context, slog.Level) bool { return true }
func (f *failedReads) WithAttrs([]slog.Attr) slog.Handler       { return f }
func (f *failedReads) WithGroup(string) slog.Handler            { return f }

func (f *failedReads) Handle(_ context.Context, r slog.Record) error {
	if r.Message == "layer read failed" {
		f.n.Add(1)
	}
	return nil
}

// A snapshotter whose volume endpoint cannot listen does not start, so no
// host runs with mounts that have no endpoint to reach.
func TestTheSnapshotterNeedsItsVolumeEndpoint(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Fatal("the snapshotter tests mount FUSE and overlay filesystems and run as root")
	}
	taken, err := (&net.ListenConfig{}).Listen(t.Context(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = taken.Close() }()
	root := t.TempDir()
	cfg := testConfig(filepath.Join(root, "state"), http.DefaultTransport, slog.New(slog.DiscardHandler))
	cfg.VolumeAddr = taken.Addr().String()
	err = Serve(t.Context(), cfg, filepath.Join(root, "snapshotter.sock"), func() { t.Error("the snapshotter served without its endpoint") })
	var listen *net.OpError
	if !errors.As(err, &listen) || listen.Op != "listen" || listen.Addr.String() != cfg.VolumeAddr {
		t.Fatalf("a snapshotter whose endpoint's address is taken: %v", err)
	}
}

func serve(t *testing.T, transport http.RoundTripper) *service {
	t.Helper()
	return serveFilling(t, transport, 0)
}

// serveFilling serves a snapshotter that fetches layers up to fillBytes
// whole once mounted.
func serveFilling(t *testing.T, transport http.RoundTripper, fillBytes int64) *service {
	t.Helper()
	// The snapshotter mounts as root, as on hosts: the CI job and
	// deploy/local/host-vm.sh run these tests as root.
	if os.Geteuid() != 0 {
		t.Fatal("the snapshotter tests mount FUSE and overlay filesystems and run as root")
	}
	root := t.TempDir()
	socket := filepath.Join(root, "snapshotter.sock")
	failures := &failedReads{}
	cfg := testConfig(filepath.Join(root, "state"), transport, slog.New(failures))
	cfg.FillBytes = fillBytes
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
	var once sync.Once
	stop := func() {
		once.Do(func() {
			_ = sources.Close()
			_ = conn.Close()
			cancel()
			if err := <-served; err != nil {
				t.Error(err)
			}
		})
	}
	t.Cleanup(func() {
		stop()
		// The snapshotter leaves its mounts at exit for the next start.
		if err := clearStale(cfg.Root); err != nil {
			t.Error(err)
		}
	})
	return &service{root: cfg.Root, sn: proxy.NewSnapshotter(snapshotsapi.NewSnapshotsClient(conn), layersource.Snapshotter), sources: sources, failures: failures, stop: stop}
}

// grant grants the snapshotter grants as a refresh does.
func (s *service) grant(t *testing.T, grants ...*imagefsproto.LayerGrant) {
	t.Helper()
	if err := s.sources.Grant(t.Context(), "", grants); err != nil {
		t.Fatal(err)
	}
}

// prepare asks for l lazily under key, as containerd's unpacker does.
func (s *service) prepare(t *testing.T, key string, l storedLayer, parent string) error {
	t.Helper()
	_, err := s.sn.Prepare(t.Context(), key, parent, snapshots.WithLabels(map[string]string{
		snapshots.LabelSnapshotRef:    "chain-" + string(l.index.Layer),
		snapshots.LabelSnapshotDiffID: string(l.index.Layer),
		layersource.LazyLabel:         "true",
	}))
	return err
}

// pull makes a lazy layer present and returns its snapshot name.
func (s *service) pull(t *testing.T, l storedLayer, parent string) string {
	t.Helper()
	key := "extract-" + uuid.NewString()
	if err := s.prepare(t, key, l, parent); !errdefs.IsAlreadyExists(err) {
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
		if fields := strings.Fields(line); len(fields) > 4 && strings.HasPrefix(fields[4], filepath.Join(root, "snapshots")) && strings.Contains(line, " fuse."+layersource.Snapshotter+" ") {
			found = append(found, fields[4])
		}
	}
	return found
}

// A lazily pulled layer is reported present without a download, and its
// mount shows every file of the tar with its bytes and metadata.
func TestLazyLayerServesItsFiles(t *testing.T) {
	t.Parallel()
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
	s.grant(t, l.grant)
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
	// overlayfs reads an opaque directory's marker, a trusted xattr only
	// root reads, from a lower layer.
	marker := make([]byte, 4)
	if n, err := unix.Lgetxattr(filepath.Join(dir, "opaque"), opaqueXattr, marker); err != nil || string(marker[:n]) != "y" {
		t.Fatalf("the opaque directory's %s: %v", opaqueXattr, err)
	}
	if _, err := unix.Lgetxattr(filepath.Join(dir, "etc"), opaqueXattr, marker); !errors.Is(err, unix.ENODATA) {
		t.Fatalf("a plain directory's %s: %v", opaqueXattr, err)
	}
	if fetched := transport.requests.Load() - pulled; fetched != int64(len(l.index.Frames)) {
		t.Fatalf("reading every file made %d store requests for %d frames", fetched, len(l.index.Frames))
	}
}

// Without a live grant a layer is not made present, and the error says so.
func TestPullingAnUngrantedLayerFails(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	s := serve(t, http.DefaultTransport)
	l := ts.file(t, "a", []byte("a"))
	if err := s.prepare(t, "extract", l, ""); !errdefs.IsFailedPrecondition(err) || !strings.Contains(err.Error(), "no live grant") {
		t.Fatalf("prepare without a grant: %v", err)
	}
	expired := &imagefsproto.LayerGrant{DiffId: l.grant.GetDiffId(), IndexUrl: l.grant.GetIndexUrl(), DataUrl: l.grant.GetDataUrl(), ExpiresAt: timestamppb.New(time.Now().Add(-time.Second))}
	s.grant(t, expired)
	if err := s.prepare(t, "extract", l, ""); !errdefs.IsFailedPrecondition(err) {
		t.Fatalf("prepare with an expired grant: %v", err)
	}
}

// A read the store cannot serve reaches the container as EIO, logged,
// after a bounded number of attempts; it never reads as wrong or empty
// bytes.
func TestUnservableReadsAreIOErrors(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	var failing atomic.Bool
	transport := &countingTransport{fail: func(*http.Request) bool { return failing.Load() }}
	s := serve(t, transport)
	l := ts.layer(t, buildTar(t, []tarEntry{
		{hdr: tar.Header{Name: "a", Typeflag: tar.TypeReg, Mode: 0o644}, body: random(1 << 20)},
		{hdr: tar.Header{Name: "b", Typeflag: tar.TypeReg, Mode: 0o644}, body: random(1 << 20)},
	}))
	s.grant(t, l.grant)
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
	if got := s.failures.n.Load(); got < 2 {
		t.Fatalf("%d read failures logged, want at least 2", got)
	}
}

// A layer stays mounted while a view or container uses it and is unmounted
// once none does; a busy mount is kept and retried.
func TestUnusedLayersAreUnmounted(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	s := serve(t, http.DefaultTransport)
	l := ts.file(t, "a", []byte("a"))
	s.grant(t, l.grant)
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
	t.Parallel()
	ts := newTestStore(t)
	s := serve(t, http.DefaultTransport)
	base := ts.file(t, "base", []byte("base"))
	top := ts.file(t, "top", []byte("top"))
	s.grant(t, base.grant, top.grant)
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

// A container whose layers fail to mount leaves none of them mounted.
func TestFailedMountsLeaveNoLayerMounted(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	s := serve(t, http.DefaultTransport)
	base := ts.file(t, "base", []byte("base"))
	top := ts.file(t, "top", []byte("top"))
	s.grant(t, base.grant, top.grant)
	upper := s.pull(t, top, s.pull(t, base, ""))
	// The base mounts after the top, and its stored index no longer reads.
	indexes, err := filepath.Glob(filepath.Join(s.root, "snapshots", "*", indexFile))
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range indexes {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if ix, err := imagefs.Unmarshal(raw); err == nil && ix.Layer == base.index.Layer {
			if err := os.WriteFile(path, []byte("broken"), 0o600); err != nil {
				t.Fatal(err)
			}
		}
	}
	if _, err := s.sn.Prepare(t.Context(), "container", upper); err == nil {
		t.Fatal("a container over a layer that cannot mount was prepared")
	}
	if got := layerMounts(t, s.root); len(got) != 0 {
		t.Fatalf("after the failed prepare layers are mounted at %v", got)
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

// Stopping the snapshotter leaves a layer a container uses mounted, and
// returns at once even with a file of it open.
func TestStopLeavesLayersInUseMounted(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	s := serve(t, http.DefaultTransport)
	l := ts.file(t, "a", []byte("a"))
	s.grant(t, l.grant)
	dir := s.view(t, "view", s.pull(t, l, ""))
	open, err := os.Open(filepath.Join(dir, "a"))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = open.Close() }()
	began := time.Now()
	s.stop()
	if took := time.Since(began); took > 5*time.Second {
		t.Fatalf("stopping took %v", took)
	}
	if got := layerMounts(t, s.root); len(got) != 1 {
		t.Fatalf("after stop the layer is mounted at %v, want left in place", got)
	}
}

// A mounted layer within the fill bound is fetched whole in the background,
// each frame once, so later reads wait for no store; a larger one is not.
func TestMountedLayersFillInTheBackground(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	transport := &countingTransport{}
	s := serveFilling(t, transport, 16<<20)
	body := random(28 << 20)
	small := ts.file(t, "small", body[:12<<20])
	large := ts.file(t, "large", body)
	s.grant(t, small.grant, large.grant)
	dir := s.view(t, "view", s.pull(t, small, ""))
	s.view(t, "view-large", s.pull(t, large, ""))
	want := int64(2 + len(small.index.Frames))
	for deadline := time.Now().Add(30 * time.Second); transport.requests.Load() < want && time.Now().Before(deadline); {
		time.Sleep(50 * time.Millisecond)
	}
	got, err := os.ReadFile(filepath.Join(dir, "small"))
	if err != nil || !bytes.Equal(got, body[:12<<20]) {
		t.Fatalf("read back %d bytes, %v", len(got), err)
	}
	if n := transport.requests.Load(); n != want {
		t.Fatalf("%d store requests, want two indexes and each frame of the small layer once (%d)", n, want)
	}
}

// The grant expiring last wins whatever order grants arrive in, and a call
// with a bad grant records none of its grants.
func TestGrantsKeepTheLatestExpiry(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	s := serve(t, http.DefaultTransport)
	l := ts.file(t, "a", []byte("a"))
	other := ts.file(t, "b", []byte("b"))
	sooner := &imagefsproto.LayerGrant{
		DiffId: string(l.index.Layer), IndexUrl: "http://127.0.0.1:1/index", DataUrl: "http://127.0.0.1:1/data", ExpiresAt: timestamppb.New(time.Now().Add(time.Minute)),
	}
	s.grant(t, l.grant)
	s.grant(t, sooner)
	s.pull(t, l, "")

	bad := &imagefsproto.LayerGrant{DiffId: "sha256:short", IndexUrl: other.grant.GetIndexUrl(), DataUrl: other.grant.GetDataUrl(), ExpiresAt: other.grant.GetExpiresAt()}
	if err := s.sources.Grant(t.Context(), "", []*imagefsproto.LayerGrant{other.grant, bad}); status.Code(errors.Unwrap(err)) != codes.InvalidArgument {
		t.Fatalf("a call with a bad digest: %v", err)
	}
	if err := s.prepare(t, "extract", other, ""); !errdefs.IsFailedPrecondition(err) {
		t.Fatalf("a refused call recorded its grants: %v", err)
	}
}

// Through mounts, a trace of one start lists the frames it read, and a
// later cold start's prefetch of that trace fetches those frames and no
// others, so the same reads then wait for no store.
func TestTracedFramesPrefetchThroughMounts(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	weights := random(5 * imagefs.FrameSize)
	base := ts.file(t, "lib", weights[:3*imagefs.FrameSize])
	app := ts.file(t, "weights", weights)
	layers := []imagefs.Digest{base.index.Layer, app.index.Layer}
	start := func(s *service) (string, string) {
		t.Helper()
		if err := s.sources.Grant(t.Context(), "c1", []*imagefsproto.LayerGrant{base.grant, app.grant}); err != nil {
			t.Fatal(err)
		}
		return s.view(t, "base", s.pull(t, base, "")), s.view(t, "app", s.pull(t, app, ""))
	}
	readAt := func(path string, off int64) {
		t.Helper()
		f, err := os.Open(path)
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = f.Close() }()
		if _, err := f.ReadAt(make([]byte, 4096), off); err != nil {
			t.Fatal(err)
		}
	}
	startup := func(baseDir, appDir string) {
		readAt(filepath.Join(appDir, "weights"), 4*imagefs.FrameSize)
		readAt(filepath.Join(baseDir, "lib"), 0)
		readAt(filepath.Join(appDir, "weights"), imagefs.FrameSize)
	}

	first := serve(t, http.DefaultTransport)
	if err := first.sources.StartTrace(t.Context(), "c1", layers); err != nil {
		t.Fatal(err)
	}
	startup(start(first))
	reads, complete, err := first.sources.EndTrace(t.Context(), "c1")
	if err != nil {
		t.Fatal(err)
	}
	if want := [][2]uint32{{1, 4}, {0, 0}, {1, 1}}; !complete || !slices.Equal(framePairs(reads), want) {
		t.Fatalf("the first start traced %v, complete %v; want %v", reads, complete, want)
	}
	first.stop()

	transport := &countingTransport{}
	cold := serve(t, transport)
	if err := cold.sources.Prefetch(t.Context(), "c1", layers, reads); err != nil {
		t.Fatal(err)
	}
	baseDir, appDir := start(cold)
	const indexes = 2
	waitFor(t, func() bool { return transport.requests.Load() >= indexes+3 })
	startup(baseDir, appDir)
	if n := transport.requests.Load(); n != indexes+3 {
		t.Fatalf("a cold start with its trace made %d store requests, want two indexes and three frames", n)
	}
}
