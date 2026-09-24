package main

import (
	"context"
	"os"
	"os/exec"
	"slices"
	"testing"
)

// A volume made from a snapshot holds the source's state: its attachment names
// another machine's device, and its head and sealed layers hold writes no
// generation recorded. Adopting must drop all of that and keep exactly the
// published chain, or the disk would start from data the control plane never
// saw, or tear down a device another disk is using here.
func TestAdoptingASnapshotKeepsOnlyItsPublishedChain(t *testing.T) {
	if _, err := exec.LookPath(toolImage); err != nil {
		t.Skip("qemu-img is not installed")
	}
	ctx := context.Background()
	p := diskPaths{root: t.TempDir(), id: "disk-1"}
	if err := os.MkdirAll(p.layerDir(), 0o700); err != nil {
		t.Fatal(err)
	}
	const size = 64 << 20
	state := &diskState{DiskID: p.id, SizeBytes: size, Published: []publishedRecord{}}
	base := state.newLayer()
	if err := createBase(ctx, p.layerPath(base), size); err != nil {
		t.Fatal(err)
	}
	base.Generation = 3
	sealed, head := state.newLayer(), state.newLayer()
	for i, l := range []layer{sealed, head} {
		below := []layer{base, sealed}[i]
		if err := createOverlay(ctx, p.layerPath(l), below, size); err != nil {
			t.Fatal(err)
		}
	}
	state.Layers = []layer{base, sealed, head}
	state.PublishedGeneration = 3
	state.PublishedManifestSHA256 = "a"
	state.Published = []publishedRecord{{Generation: 3, ManifestKey: "k", ManifestSHA256: "a"}}
	state.Attachment = &attachment{Mountpoint: "/mnt/other", DaemonPID: 1, Device: "/dev/nbd7", Mounted: true}
	if err := saveState(p, state); err != nil {
		t.Fatal(err)
	}

	result, err := runSnapshotPoint(ctx, []string{"--root", p.root, "--disk", p.id, "--volume", "vol-source"})
	if err != nil {
		t.Fatal(err)
	}
	if point := result.(snapshotPointResult); point.Generation != 3 {
		t.Fatalf("snapshot point at generation %d, want 3", point.Generation)
	}

	// On the volume the point was taken on, nothing is adopted.
	same, adopted, err := adoptSnapshot(ctx, p, state, "vol-source")
	if err != nil || adopted || same != state {
		t.Fatalf("the source volume adopted its own point: %v, %v", adopted, err)
	}

	copied, adopted, err := adoptSnapshot(ctx, p, state, "vol-copy")
	if err != nil || !adopted {
		t.Fatalf("a copy did not adopt the point: %v, %v", adopted, err)
	}
	if copied.Attachment != nil || !copied.HeadFresh || !copied.Hydrating {
		t.Fatalf("adopted state kept the source's attachment or head: %+v", copied)
	}
	if len(copied.Layers) != 2 || copied.Layers[0] != base || copied.Layers[1].Generation != 0 {
		t.Fatalf("adopted layers %+v, want the published base under a fresh head", copied.Layers)
	}
	var files []string
	entries, err := os.ReadDir(p.layerDir())
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		files = append(files, entry.Name())
	}
	if want := []string{base.file(), copied.Layers[1].file()}; !slices.Equal(files, want) {
		t.Fatalf("layer files %v, want %v", files, want)
	}

	// A retried attach on the copy keeps what the first one built.
	again, adopted, err := adoptSnapshot(ctx, p, copied, "vol-copy")
	if err != nil || adopted || again != copied {
		t.Fatalf("a retried attach adopted the point again: %v, %v", adopted, err)
	}
}
