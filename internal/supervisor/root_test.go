package supervisor

import (
	"os"
	"path/filepath"
	"testing"
)

// A devbox root that runs out of space while seeding names the disk's
// size, the image's and the size to set, counting the image as the seed
// copies it: without mount points.
func TestAFullRootNamesTheSizesToChange(t *testing.T) {
	base := t.TempDir()
	sized := func(path string, bytes int64) {
		t.Helper()
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		f, err := os.Create(path)
		if err != nil {
			t.Fatal(err)
		}
		if err := f.Truncate(bytes); err != nil {
			t.Fatal(err)
		}
		if err := f.Close(); err != nil {
			t.Fatal(err)
		}
	}
	sized(filepath.Join(base, "usr/lib/model.bin"), 11<<30)
	sized(filepath.Join(base, "usr/bin/tool"), 512<<20)
	sized(filepath.Join(base, "data/volume.bin"), 100<<30)
	if err := os.Symlink("usr/bin/tool", filepath.Join(base, "tool")); err != nil {
		t.Fatal(err)
	}
	image := treeBytes(base, map[string]bool{filepath.Join(base, "data"): true})
	got := rootTooSmall(10<<30, image).Error()
	if want := "the root disk of 10 GiB is too small for its image of 11.5 GiB; set disk to at least 14 GiB"; got != want {
		t.Fatalf("message %q, want %q", got, want)
	}
	if got, want := rootTooSmall(10<<30, 1<<30).Error(), "the root disk of 10 GiB is too small for its image of 1 GiB; set disk to at least 10 GiB"; got != want {
		t.Fatalf("message %q, want %q", got, want)
	}
}
