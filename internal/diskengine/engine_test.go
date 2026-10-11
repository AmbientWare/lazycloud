package diskengine

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"syscall"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
)

const (
	frame         = imagefs.FrameSize
	testDiskBytes = 16 * frame
)

// A disk publishes only the frames its writes touched, and its stack moves
// onto each committed generation while writes continue. Another host with
// an empty cache attaches the newest generation, reading only the frames
// it touches, and sees the same bytes.
func TestPublishedGenerationRestoresOnAnotherHost(t *testing.T) {
	requireTools(t, toolDaemon, toolImage, toolRunUnit, "qemu-io")
	ctx := t.Context()
	store := testStore(t)
	diskID := uuid.NewString()
	first := newHost(t, 64*frame)
	req := AttachRequest{DiskID: diskID, SizeBytes: testDiskBytes, Mountpoint: "/unused"}

	p, state, attached := attachUnmounted(t, first, store, req)
	if attached.Reused || !state.Unformatted {
		t.Fatalf("a new disk attached as %+v", attached)
	}
	if sealUnmounted(t, p, state) {
		t.Fatal("sealed a head nothing wrote")
	}
	// Two frames, one only partly.
	writeExport(t, p, frame+frame/2, frame, 0xa1)
	if !sealUnmounted(t, p, state) {
		t.Fatal("did not seal a written head")
	}
	one, err := first.engine.Publish(ctx, diskID, store, false)
	if err != nil {
		t.Fatal(err)
	}
	if one == nil || one.Generation != 1 || one.AddedBytes <= 0 {
		t.Fatalf("first publish %+v", one)
	}
	if retried, err := first.engine.Publish(ctx, diskID, store, false); err != nil || *retried != *one {
		t.Fatalf("a retried publish returned %+v, %v; the first %+v", retried, err, one)
	}
	if got := reload(t, p).Pending.Dirty; !slices.Equal(got, []uint32{1, 2}) {
		t.Fatalf("generation 1 replaced frames %v, want the two written", got)
	}
	if err := first.engine.CommitPublished(ctx, diskID, 1); err != nil {
		t.Fatal(err)
	}
	if err := first.engine.CommitPublished(ctx, diskID, 1); err != nil {
		t.Fatalf("committing the committed generation again: %v", err)
	}
	if err := first.engine.CommitPublished(ctx, diskID, 5); !errors.Is(err, ErrInvalid) {
		t.Fatalf("committing a generation never uploaded returned %v", err)
	}
	if n := frameCount(t, store, diskID); n != 2 {
		t.Fatalf("generation 1 stored %d frames, want 2", n)
	}

	// One more frame changes; the next generation stores it alone, and the
	// stack moves onto it while another range is being written.
	state = reload(t, p)
	writeExport(t, p, 9*frame, 4096, 0xb2)
	if !sealUnmounted(t, p, state) {
		t.Fatal("did not seal the second write")
	}
	two, err := first.engine.Publish(ctx, diskID, store, false)
	if err != nil {
		t.Fatal(err)
	}
	if got := reload(t, p).Pending.Dirty; !slices.Equal(got, []uint32{9}) {
		t.Fatalf("generation 2 replaced frames %v, want [9]", got)
	}
	writing := make(chan error, 1)
	go func() {
		out, err := exec.CommandContext(ctx, "qemu-io", "-f", "raw", "-c", fmt.Sprintf("write -P 0xc3 %d %d", 12*frame, 3*frame), nbdURI(p)).CombinedOutput()
		if err != nil {
			err = fmt.Errorf("%w: %s", err, out)
		}
		writing <- err
	}()
	if err := first.engine.CommitPublished(ctx, diskID, two.Generation); err != nil {
		t.Fatal(err)
	}
	if err := <-writing; err != nil {
		t.Fatal(err)
	}
	if n := frameCount(t, store, diskID); n != 3 {
		t.Fatalf("after generation 2 the disk stores %d frames, want 3", n)
	}
	state = reload(t, p)
	if len(state.Layers) != 1 || state.Base.Generation != 2 {
		t.Fatalf("after the rebase the stack is %+v on %+v", state.Layers, state.Base)
	}
	want := make([]byte, testDiskBytes)
	copy(want[frame+frame/2:], bytes.Repeat([]byte{0xa1}, frame))
	copy(want[9*frame:], bytes.Repeat([]byte{0xb2}, 4096))
	copy(want[12*frame:], bytes.Repeat([]byte{0xc3}, 3*frame))
	requireSameDisk(t, readExport(t, p), want)

	// The writes made during the rebase publish too, their three equal
	// frames stored once; the indexes they replace go once collected.
	if !sealUnmounted(t, p, state) {
		t.Fatal("did not seal the writes made during the rebase")
	}
	three := publishAndCommit(t, first.engine, diskID, store, false)
	if err := first.engine.Collect(ctx, diskID, storeRemover(store)); err != nil {
		t.Fatal(err)
	}
	if n := frameCount(t, store, diskID); n != 4 {
		t.Fatalf("after collecting, generation 3 stores %d frames, want 4", n)
	}

	second := newHost(t, 64*frame)
	other := req
	other.Base = baseOf(three)
	q, _, restored := attachUnmounted(t, second, store, other)
	if restored.Reused || restored.Generation != 3 {
		t.Fatalf("attach on another host returned %+v", restored)
	}
	// The prefetch had no start trace or recent frames to fetch.
	before := second.store.requests.Load()
	run(t, "qemu-io", "-f", "raw", "-c", fmt.Sprintf("read -P 0xb2 %d 4096", 9*frame), nbdURI(q))
	if fetched := second.store.requests.Load() - before; fetched != 1 {
		t.Fatalf("reading one frame fetched %d objects", fetched)
	}
	requireSameDisk(t, readExport(t, q), want)
}

// The frame cache yields to the disk's own writes: reading more of the base
// than the cache holds never loses what the stack holds unpublished.
func TestEvictionNeverTouchesUnpublishedWrites(t *testing.T) {
	requireTools(t, toolDaemon, toolImage, toolRunUnit, "qemu-io")
	store := testStore(t)
	diskID := uuid.NewString()
	writer := newHost(t, 64*frame)
	req := AttachRequest{DiskID: diskID, SizeBytes: testDiskBytes, Mountpoint: "/unused"}
	p, state, _ := attachUnmounted(t, writer, store, req)
	for i := range int64(16) {
		writeExport(t, p, i*frame, frame, byte(0x10+i))
	}
	sealUnmounted(t, p, state)
	base := publishAndCommit(t, writer.engine, diskID, store, false)
	writer.stop()

	// Two frames of cache for sixteen of base.
	small := newHost(t, 2*frame)
	req.Base = baseOf(base)
	q, qstate, _ := attachUnmounted(t, small, store, req)
	writeExport(t, q, 3*frame, 4096, 0xee)
	want := readExport(t, q)
	for range 2 {
		requireSameDisk(t, readExport(t, q), want)
	}
	if !sealUnmounted(t, q, qstate) {
		t.Fatal("the write did not reach the head")
	}
	published := publishAndCommit(t, small.engine, diskID, store, false)
	if published.Generation != 2 {
		t.Fatalf("publish after eviction %+v", published)
	}
	requireSameDisk(t, readExport(t, q), want)
}

// TestAttachMountsAndRestores runs the public lifecycle through a kernel
// NBD device and an ext4 mount, then attaches the published disk grown on a
// host with an empty cache. It needs root and the nbd module.
func TestAttachMountsAndRestores(t *testing.T) {
	if err := Check(); err != nil {
		t.Skip(err)
	}
	ctx := t.Context()
	store := testStore(t)
	diskID := uuid.NewString()
	h := newHost(t, 64*frame)
	mountpoint := filepath.Join(t.TempDir(), "mnt")
	req := AttachRequest{DiskID: diskID, SizeBytes: 1 << 30, Mountpoint: mountpoint}
	h.grant(t, store, diskID)
	t.Cleanup(func() { _ = h.engine.Detach(context.Background(), diskID) })

	attached, err := h.engine.Attach(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	if !attached.Formatted || attached.Reused {
		t.Fatalf("a new disk attached as %+v", attached)
	}
	if again, err := h.engine.Attach(ctx, req); err != nil || !again.Reused {
		t.Fatalf("attaching an attached disk again returned %+v, %v", again, err)
	}
	content := bytes.Repeat([]byte("lazycloud"), 1_000_000)
	if err := os.WriteFile(filepath.Join(mountpoint, "data"), content, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := h.engine.Seal(ctx, diskID); err != nil {
		t.Fatal(err)
	}
	published := publishAndCommit(t, h.engine, diskID, store, true)
	if err := h.engine.Seal(ctx, diskID); err != nil {
		t.Fatal(err)
	}
	if idle, err := h.engine.Publish(ctx, diskID, store, false); err != nil || idle != nil {
		t.Fatalf("publishing an idle disk returned %+v, %v", idle, err)
	}
	if err := h.engine.Detach(ctx, diskID); err != nil {
		t.Fatal(err)
	}

	other := newHost(t, 64*frame)
	other.grant(t, store, diskID)
	t.Cleanup(func() { _ = other.engine.Detach(context.Background(), diskID) })
	req.Base, req.SizeBytes = baseOf(published), 2<<30
	restored, err := other.engine.Attach(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	if restored.Reused || restored.Formatted || restored.Generation != published.Generation {
		t.Fatalf("restore returned %+v", restored)
	}
	got, err := os.ReadFile(filepath.Join(mountpoint, "data"))
	if err != nil {
		t.Fatal(err)
	}
	if sha256.Sum256(got) != sha256.Sum256(content) {
		t.Fatal("the restored file differs from the one written")
	}
	var fs syscall.Statfs_t
	if err := syscall.Statfs(mountpoint, &fs); err != nil || int64(fs.Blocks)*fs.Bsize < 1<<30 { //nolint:gosec // Test sizes.
		t.Fatalf("the filesystem did not grow with the disk: %+v %v", fs, err)
	}
	if err := other.engine.Detach(ctx, diskID); err != nil {
		t.Fatal(err)
	}
}

// When the snapshotter serving a disk's base stops, the disk's status says
// so, and a seal releases the attachment keeping what reached the head.
// It needs root and the nbd module.
func TestSnapshotterDeathLosesTheAttachment(t *testing.T) {
	if err := Check(); err != nil {
		t.Skip(err)
	}
	ctx := t.Context()
	store := testStore(t)
	diskID := uuid.NewString()
	h := newHost(t, 64*frame)
	mountpoint := filepath.Join(t.TempDir(), "mnt")
	req := AttachRequest{DiskID: diskID, SizeBytes: 1 << 30, Mountpoint: mountpoint}
	h.grant(t, store, diskID)
	if _, err := h.engine.Attach(ctx, req); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(mountpoint, "data"), []byte("before"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := h.engine.Seal(ctx, diskID); err != nil {
		t.Fatal(err)
	}
	published := publishAndCommit(t, h.engine, diskID, store, false)
	if err := h.engine.Detach(ctx, diskID); err != nil {
		t.Fatal(err)
	}

	req.Base = baseOf(published)
	h.grant(t, store, diskID)
	if _, err := h.engine.Attach(ctx, req); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = h.engine.Detach(context.Background(), diskID) })
	if _, _, err := h.engine.Status(diskID); err != nil {
		t.Fatalf("a served disk's status: %v", err)
	}
	h.stop()
	_, _, err := h.engine.Status(diskID)
	if !errors.Is(err, ErrAttachmentLost) || !bytes.Contains([]byte(err.Error()), []byte("snapshotter")) {
		t.Fatalf("status after the snapshotter stopped: %v, want ErrAttachmentLost naming the snapshotter", err)
	}
}

// The daemon runs in a systemd scope of its own, so the service that
// attached the disk can stop or restart while the disk stays served.
func TestDaemonRunsOutsideTheCallersCgroup(t *testing.T) {
	requireTools(t, toolDaemon, toolImage, toolRunUnit)
	h := newHost(t, 64*frame)
	_, state, _ := attachUnmounted(t, h, testStore(t), AttachRequest{DiskID: uuid.NewString(), SizeBytes: testDiskBytes, Mountpoint: "/unused"})
	ours, err := os.ReadFile("/proc/self/cgroup")
	if err != nil {
		t.Fatal(err)
	}
	daemon, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(state.Attachment.DaemonPID), "cgroup"))
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Equal(daemon, ours) || !bytes.Contains(daemon, []byte("/lazycloud-disk-"+state.DiskID+"-")) {
		t.Fatalf("the daemon runs in cgroup %s; the caller in %s", daemon, ours)
	}
}

// A disk whose daemon died is recovered by the next seal: what reached the
// head is sealed and publishes, and the disk is detached.
func TestSealRecoversALostDaemon(t *testing.T) {
	requireTools(t, toolDaemon, toolImage, toolRunUnit, "qemu-io")
	ctx := t.Context()
	store := testStore(t)
	diskID := uuid.NewString()
	h := newHost(t, 64*frame)
	req := AttachRequest{DiskID: diskID, SizeBytes: testDiskBytes, Mountpoint: "/unused"}
	p, state, _ := attachUnmounted(t, h, store, req)
	writeExport(t, p, 2*frame, 3*frame, 0x5a)
	want := readExport(t, p)
	if err := syscall.Kill(state.Attachment.DaemonPID, syscall.SIGKILL); err != nil {
		t.Fatal(err)
	}
	for daemonAlive(p, state.Attachment.DaemonPID) {
		time.Sleep(10 * time.Millisecond)
	}

	if err := h.engine.Seal(ctx, diskID); !errors.Is(err, ErrAttachmentLost) {
		t.Fatalf("sealing a disk whose daemon died returned %v, want ErrAttachmentLost", err)
	}
	if state = reload(t, p); state.Attachment != nil || !state.HeadFresh {
		t.Fatalf("after recovering, the disk is %+v", state)
	}
	if err := h.engine.Seal(ctx, diskID); err != nil {
		t.Fatalf("sealing the recovered disk again: %v", err)
	}
	published := publishAndCommit(t, h.engine, diskID, store, false)
	restoredReq := req
	restoredReq.Base = baseOf(published)
	q, _, _ := attachUnmounted(t, newHost(t, 64*frame), store, restoredReq)
	requireSameDisk(t, readExport(t, q), want)
}

// The engine refuses a malformed request before touching anything.
func TestAttachRefusesMalformedRequests(t *testing.T) {
	e := New(t.TempDir(), nil, nil)
	valid := AttachRequest{DiskID: "0b6f6c3e-5d0a-4c55-9a51-2f1f4c1d7e10", SizeBytes: 1 << 30, Mountpoint: "/mnt/d"}
	for name, mutate := range map[string]func(*AttachRequest){
		"unaligned size":    func(r *AttachRequest) { r.SizeBytes = 1<<30 + 512 },
		"zero size":         func(r *AttachRequest) { r.SizeBytes = 0 },
		"relative mount":    func(r *AttachRequest) { r.Mountpoint = "mnt/d" },
		"disk id with dots": func(r *AttachRequest) { r.DiskID = "../etc" },
		"base without a digest": func(r *AttachRequest) {
			r.Base = &Generation{Generation: 1}
		},
	} {
		req := valid
		mutate(&req)
		if _, err := e.Attach(t.Context(), req); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: got %v, want ErrInvalid", name, err)
		}
	}
}

// A final publish after the snapshotter forgot the disk, as after it
// restarted, keeps the recent frames the last generation recorded and
// publishes nothing when nothing else changed.
func TestFinalPublishKeepsRecentFramesTheSnapshotterForgot(t *testing.T) {
	requireTools(t, toolDaemon, toolImage, toolRunUnit, "qemu-io")
	ctx := t.Context()
	store := testStore(t)
	diskID := uuid.NewString()
	h := newHost(t, 64*frame)
	p, state, _ := attachUnmounted(t, h, store, AttachRequest{DiskID: diskID, SizeBytes: testDiskBytes, Mountpoint: "/unused"})
	writeExport(t, p, 0, frame, 0x11)
	writeExport(t, p, 3*frame, frame, 0x22)
	sealUnmounted(t, p, state)
	publishAndCommit(t, h.engine, diskID, store, false)
	readExport(t, p)
	publishAndCommit(t, h.engine, diskID, store, true)
	if recent := readIndex(t, p).Recent; len(recent) != 2 {
		t.Fatalf("the final publish recorded recent frames %v, want the two read", recent)
	}
	if err := h.bases.ReleaseDisk(ctx, diskID, 0); err != nil {
		t.Fatal(err)
	}
	if again, err := h.engine.Publish(ctx, diskID, store, true); err != nil || again != nil {
		t.Fatalf("a final publish with nothing new returned %+v, %v", again, err)
	}
}

// Detaching a disk whose attach kept nothing, as one that failed before
// serving it, has the snapshotter drop the disk's grant.
func TestDetachDropsTheGrantOfADiskNeverServed(t *testing.T) {
	h := newHost(t, 64*frame)
	diskID := uuid.NewString()
	h.grant(t, testStore(t), diskID)
	if _, _, err := h.bases.DiskReads(t.Context(), diskID); err != nil {
		t.Fatalf("a granted disk's reads: %v", err)
	}
	if err := h.engine.Detach(t.Context(), diskID); err != nil {
		t.Fatal(err)
	}
	if _, _, err := h.bases.DiskReads(t.Context(), diskID); status.Code(err) != codes.NotFound {
		t.Fatalf("after the detach the snapshotter answers %v, want NotFound", err)
	}
}

// A stalled disk's writers wait until it resumes, and it still seals. It
// needs root and the nbd module.
func TestStalledDiskWritesWaitUntilResumed(t *testing.T) {
	if err := Check(); err != nil {
		t.Skip(err)
	}
	ctx := t.Context()
	diskID := uuid.NewString()
	h := newHost(t, 64*frame)
	mountpoint := filepath.Join(t.TempDir(), "mnt")
	h.grant(t, testStore(t), diskID)
	t.Cleanup(func() { _ = h.engine.Detach(context.Background(), diskID) })
	if _, err := h.engine.Attach(ctx, AttachRequest{DiskID: diskID, SizeBytes: 1 << 30, Mountpoint: mountpoint}); err != nil {
		t.Fatal(err)
	}
	if err := h.engine.Stall(ctx, diskID, true); err != nil {
		t.Fatal(err)
	}
	if _, stalled, err := h.engine.Status(diskID); err != nil || !stalled {
		t.Fatalf("a stalled disk's status: stalled %v, %v", stalled, err)
	}
	wrote := make(chan error, 1)
	go func() { wrote <- os.WriteFile(filepath.Join(mountpoint, "data"), []byte("waited"), 0o600) }()
	select {
	case err := <-wrote:
		t.Fatalf("a write to a stalled disk finished: %v", err)
	case <-time.After(300 * time.Millisecond):
	}
	if err := h.engine.Seal(ctx, diskID); err != nil {
		t.Fatalf("sealing a stalled disk: %v", err)
	}
	if err := h.engine.Stall(ctx, diskID, false); err != nil {
		t.Fatal(err)
	}
	if err := <-wrote; err != nil {
		t.Fatal(err)
	}
}

// readIndex is the index of the generation the disk's stack is on.
func readIndex(t *testing.T, p diskPaths) imagefs.DiskIndex {
	t.Helper()
	raw, err := os.ReadFile(p.baseIndex())
	if err != nil {
		t.Fatal(err)
	}
	ix, err := imagefs.UnmarshalDisk(raw)
	if err != nil {
		t.Fatal(err)
	}
	return ix
}
