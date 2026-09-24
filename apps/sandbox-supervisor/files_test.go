package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestListingStopsAtItsLimitAndKeepsDanglingLinks(t *testing.T) {
	root := t.TempDir()
	for _, name := range []string{"a", "b", "c"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte(name), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink(filepath.Join(root, "gone"), filepath.Join(root, "link")); err != nil {
		t.Fatal(err)
	}

	bounded, err := filesystemOperation(filesystemRequest{Operation: "list-files", Path: root, Limit: 2})
	if err != nil {
		t.Fatal(err)
	}
	if len(bounded.Files) != 2 || !bounded.Truncated {
		t.Fatalf("bounded listing returned %d files, truncated=%v", len(bounded.Files), bounded.Truncated)
	}

	full, err := filesystemOperation(filesystemRequest{Operation: "list-files", Path: root})
	if err != nil {
		t.Fatal(err)
	}
	if len(full.Files) != 4 || full.Truncated {
		t.Fatalf("unbounded listing returned %d files, truncated=%v", len(full.Files), full.Truncated)
	}
}

func TestDownloadRefusesAFileOverItsLimit(t *testing.T) {
	path := filepath.Join(t.TempDir(), "data")
	if err := os.WriteFile(path, []byte("12345"), 0o600); err != nil {
		t.Fatal(err)
	}
	stdout := os.Stdout
	sink, err := os.OpenFile(os.DevNull, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = sink
	defer func() {
		os.Stdout = stdout
		sink.Close()
	}()

	if _, err := filesystemOperation(filesystemRequest{Operation: "download-file", Path: path, Limit: 5}); err != nil {
		t.Fatalf("a file at the limit was refused: %v", err)
	}
	if _, err := filesystemOperation(filesystemRequest{Operation: "download-file", Path: path, Limit: 4}); err == nil {
		t.Fatal("a file over the limit was written")
	}
}
