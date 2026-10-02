package diskengine

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/google/uuid"
)

const testDiskBytes = 64 << 20

func chainOf(state *diskState) []Generation {
	chain := make([]Generation, 0, len(state.Published))
	for _, record := range state.Published {
		chain = append(chain, Generation{record.Generation, record.ManifestKey, record.ManifestSHA256})
	}
	return chain
}

func publishAndCommit(t *testing.T, e *Engine, p diskPaths, state *diskState, store Store, flat bool) *Published {
	t.Helper()
	published, err := publishOldest(t.Context(), p, state, store, flat)
	if err != nil {
		t.Fatal(err)
	}
	if published == nil {
		t.Fatal("nothing awaited publishing")
	}
	if err := e.CommitPublished(p.id, published.Generation); err != nil {
		t.Fatal(err)
	}
	return published
}

// A disk written through its daemon seals, publishes, compacts, recovers and
// restores on another root with the same contents, and a flattened
// generation lets collect drop everything older. Only the kernel NBD device
// and the mount are left out; the test writes and reads the daemon's export.
func TestPublishedChainRestoresElsewhere(t *testing.T) {
	requireTools(t, toolDaemon, toolImage, "qemu-io")
	ctx := t.Context()
	store := testStore(t)
	objects := mustOpenStore(t, store)
	diskID := uuid.NewString()
	e := testEngine(t)
	req := AttachRequest{DiskID: diskID, SizeBytes: testDiskBytes, Mountpoint: "/unused", Store: store}

	p, state, result := attachUnmounted(t, e, req)
	if result.Reused || !state.Unformatted {
		t.Fatalf("a new disk attached as %+v", result)
	}
	if sealUnmounted(t, p, state) {
		t.Fatal("sealed a head nothing wrote")
	}
	writeExport(t, p, 1<<20, 3<<20, 0xa1)
	if !sealUnmounted(t, p, state) {
		t.Fatal("did not seal a written head")
	}
	if sealUnmounted(t, p, state) {
		t.Fatal("sealed again with nothing written since")
	}

	first, err := publishOldest(ctx, p, state, store, false)
	if err != nil {
		t.Fatal(err)
	}
	if first.Generation != 1 || first.ParentGeneration != 0 || first.AddedBytes < 3<<20 {
		t.Fatalf("first publish %+v", first)
	}
	retried, err := publishOldest(ctx, p, state, store, false)
	if err != nil {
		t.Fatal(err)
	}
	if *retried != *first {
		t.Fatalf("a retried publish returned %+v, the first %+v", retried, first)
	}
	if err := e.CommitPublished(diskID, 1); err != nil {
		t.Fatal(err)
	}
	if err := e.CommitPublished(diskID, 1); err != nil {
		t.Fatalf("committing the committed generation again: %v", err)
	}
	if err := e.CommitPublished(diskID, 5); !errors.Is(err, ErrInvalid) {
		t.Fatalf("committing a generation never uploaded returned %v", err)
	}

	state = reload(t, p)
	writeExport(t, p, 9<<20, 2<<20, 0xb2)
	writeExport(t, p, 1<<20, 4096, 0x00)
	if !sealUnmounted(t, p, state) {
		t.Fatal("did not seal the second write")
	}
	second := publishAndCommit(t, e, p, state, store, false)
	if second.Generation != 2 || second.ParentGeneration != 1 {
		t.Fatalf("second publish %+v", second)
	}

	// Compaction commits generation 2 into the base under the running daemon.
	state = reload(t, p)
	writeExport(t, p, 20<<20, 1<<20, 0xc3)
	want := readExport(t, p)
	compacted, err := compactLayers(ctx, p, state)
	if err != nil {
		t.Fatal(err)
	}
	if compacted != 1 || len(state.Layers) != 2 || state.Layers[0].Generation != 2 {
		t.Fatalf("compacted %d layers into %+v", compacted, state.Layers)
	}
	requireSameDisk(t, readExport(t, p), want)

	// The agent dies with the head unsealed. Recover seals it without the
	// daemon and a detached publish uploads it.
	if err := e.Recover(ctx, diskID); err != nil {
		t.Fatal(err)
	}
	if daemonAlive(p, state.Attachment.DaemonPID) {
		t.Fatal("recover left the daemon running")
	}
	third, err := e.Publish(ctx, diskID, store)
	if err != nil {
		t.Fatal(err)
	}
	if third == nil || third.Generation != 3 || third.ParentGeneration != 2 {
		t.Fatalf("publishing the recovered head returned %+v", third)
	}
	if err := e.CommitPublished(diskID, 3); err != nil {
		t.Fatal(err)
	}
	if again, err := e.Publish(ctx, diskID, store); err != nil || again != nil {
		t.Fatalf("publishing with nothing new returned %+v, %v", again, err)
	}

	// The compacted local chain no longer has generation 1 and 2 as files,
	// but the bucket does.
	state = reload(t, p)
	chain := chainOf(state)
	if len(chain) != 3 {
		t.Fatalf("committed chain %+v", chain)
	}
	other := testEngine(t)
	restoredReq := req
	restoredReq.Chain = chain
	q, _, restored := attachUnmounted(t, other, restoredReq)
	if restored.Reused || restored.Generation != 3 {
		t.Fatalf("restore on another root returned %+v", restored)
	}
	requireSameDisk(t, readExport(t, q), want)
	if err := other.Detach(ctx, diskID); err != nil {
		t.Fatal(err)
	}

	// The original root holds generation 3 and reuses it, growing the disk.
	grown := restoredReq
	grown.SizeBytes = 2 * testDiskBytes
	p, state, reused := attachUnmounted(t, e, grown)
	if !reused.Reused || !state.GrowFilesystem {
		t.Fatalf("reattaching the current local copy returned %+v", reused)
	}
	got := readExport(t, p)
	requireSameDisk(t, got[:testDiskBytes], want)
	if !bytes.Equal(got[testDiskBytes:], make([]byte, testDiskBytes)) {
		t.Fatal("the grown range is not zero")
	}

	// A flattened generation holds the whole disk and needs no parent.
	writeExport(t, p, 100<<20, 1<<20, 0xd4)
	if !sealUnmounted(t, p, state) {
		t.Fatal("did not seal the write after growing")
	}
	flat := publishAndCommit(t, e, p, state, store, true)
	if flat.Generation != 4 || flat.ParentGeneration != 0 || !flat.Flat {
		t.Fatalf("flattened publish %+v", flat)
	}
	wantFlat := readExport(t, p)
	if err := e.Detach(ctx, diskID); err != nil {
		t.Fatal(err)
	}

	flatChain := []Generation{{flat.Generation, flat.ManifestKey, flat.ManifestSHA256}}
	removed, err := e.Collect(ctx, diskID, store, flatChain)
	if err != nil {
		t.Fatal(err)
	}
	if removed <= 0 {
		t.Fatalf("collect removed %d bytes after a flatten", removed)
	}
	for _, entry := range chain {
		if present, err := objects.exists(ctx, entry.ManifestKey); err != nil || present {
			t.Fatalf("manifest of generation %d survived collect: %v", entry.Generation, err)
		}
	}

	// The flattened generation restores as a raw base, and a later layer
	// compacts into that base.
	third2 := testEngine(t)
	flatReq := grown
	flatReq.Chain = flatChain
	r, rstate, _ := attachUnmounted(t, third2, flatReq)
	if !rstate.Layers[0].Raw {
		t.Fatalf("flattened generation restored as %+v", rstate.Layers)
	}
	requireSameDisk(t, readExport(t, r), wantFlat)
	writeExport(t, r, 2<<20, 1<<20, 0xe5)
	if !sealUnmounted(t, r, rstate) {
		t.Fatal("did not seal a write over the raw base")
	}
	publishAndCommit(t, third2, r, rstate, store, false)
	rstate = reload(t, r)
	wantAfter := readExport(t, r)
	if compacted, err := compactLayers(ctx, r, rstate); err != nil || compacted != 1 {
		t.Fatalf("compacting into the raw base: %d layers, %v", compacted, err)
	}
	requireSameDisk(t, readExport(t, r), wantAfter)
	if err := third2.Detach(ctx, diskID); err != nil {
		t.Fatal(err)
	}

	// A detached disk with everything committed is safe to evict.
	disks, err := e.List(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(disks) != 1 || disks[0].Attached || disks[0].Unpublished || disks[0].LocalBytes == 0 {
		t.Fatalf("listed %+v", disks)
	}
	if err := e.Evict(diskID); err != nil {
		t.Fatal(err)
	}
	p, err = e.paths(diskID)
	if err != nil {
		t.Fatal(err)
	}
	if used, err := allocatedBytes(p.dir()); err != nil || used != 0 {
		t.Fatalf("an evicted disk uses %d bytes: %v", used, err)
	}
}

// TestAttachMountsAndRestores runs the whole public lifecycle through a
// kernel NBD device and an ext4 mount. It needs root and the nbd module.
func TestAttachMountsAndRestores(t *testing.T) {
	e := testEngine(t)
	if err := e.Check(); err != nil {
		t.Skip(err)
	}
	ctx := t.Context()
	store := testStore(t)
	diskID := uuid.NewString()
	mountpoint := filepath.Join(t.TempDir(), "mnt")
	req := AttachRequest{DiskID: diskID, SizeBytes: testDiskBytes, Mountpoint: mountpoint, Store: store}
	t.Cleanup(func() { _ = e.Detach(t.Context(), diskID) })

	attached, err := e.Attach(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	if !attached.Formatted || attached.Reused {
		t.Fatalf("a new disk attached as %+v", attached)
	}
	if again, err := e.Attach(ctx, req); err != nil || !again.Reused {
		t.Fatalf("attaching an attached disk again returned %+v, %v", again, err)
	}
	content := bytes.Repeat([]byte("lazycloud"), 100_000)
	if err := os.WriteFile(filepath.Join(mountpoint, "data"), content, 0o600); err != nil {
		t.Fatal(err)
	}
	published, err := e.Publish(ctx, diskID, store)
	if err != nil {
		t.Fatal(err)
	}
	if published == nil || published.Generation != 1 {
		t.Fatalf("publish returned %+v", published)
	}
	if err := e.CommitPublished(diskID, published.Generation); err != nil {
		t.Fatal(err)
	}
	if idle, err := e.Publish(ctx, diskID, store); err != nil || idle != nil {
		t.Fatalf("publishing an idle disk returned %+v, %v", idle, err)
	}
	if err := e.Detach(ctx, diskID); err != nil {
		t.Fatal(err)
	}
	if err := e.Evict(diskID); err != nil {
		t.Fatal(err)
	}

	req.Chain = []Generation{{published.Generation, published.ManifestKey, published.ManifestSHA256}}
	req.SizeBytes = 2 * testDiskBytes
	restored, err := e.Attach(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	if restored.Reused || restored.Formatted || restored.Generation != 1 {
		t.Fatalf("restore returned %+v", restored)
	}
	got, err := os.ReadFile(filepath.Join(mountpoint, "data"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, content) {
		t.Fatal("the restored file differs from the one written")
	}
	if err := e.Detach(ctx, diskID); err != nil {
		t.Fatal(err)
	}
}
