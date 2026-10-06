package agent

import (
	"archive/tar"
	"bytes"
	"os"
	"path/filepath"
	"slices"
	"testing"
)

// A synced module replaces its cached bytecode: Python would trust the old
// cache when the edit keeps the size and lands in the same second.
func TestSyncDropsTheBytecodeOfChangedModules(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"app.py", "gone.py", "other.py",
		"__pycache__/app.cpython-312.pyc", "__pycache__/app.cpython-312.opt-1.pyc",
		"__pycache__/gone.cpython-312.pyc", "__pycache__/other.cpython-312.pyc", "__pycache__/appendix.cpython-312.pyc"} {
		if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, name)), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, name), []byte("v1"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	var archive bytes.Buffer
	w := tar.NewWriter(&archive)
	if err := w.WriteHeader(&tar.Header{Name: "app.py", Mode: 0o644, Size: 2, Typeflag: tar.TypeReg}); err != nil {
		t.Fatal(err)
	}
	if _, err := w.Write([]byte("v2")); err != nil {
		t.Fatal(err)
	}
	if err := w.WriteHeader(&tar.Header{Name: "gone.py", Typeflag: tar.TypeReg, PAXRecords: map[string]string{removedRecord: "1"}}); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	if written, removed, err := applySync(dir, &archive); err != nil || written != 1 || removed != 1 {
		t.Fatalf("sync wrote %d and removed %d (%v), want 1 and 1", written, removed, err)
	}
	entries, err := os.ReadDir(filepath.Join(dir, "__pycache__"))
	if err != nil {
		t.Fatal(err)
	}
	var left []string
	for _, e := range entries {
		left = append(left, e.Name())
	}
	if want := []string{"appendix.cpython-312.pyc", "other.cpython-312.pyc"}; !slices.Equal(left, want) {
		t.Fatalf("bytecode cache holds %q, want %q", left, want)
	}
}
