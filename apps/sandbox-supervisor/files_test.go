package main

import (
	"bytes"
	"errors"
	"math"
	"os"
	"path/filepath"
	"testing"
)

func TestBoundedListingReturnsTheFirstNamesInOrderAndKeepsDanglingLinks(t *testing.T) {
	root := t.TempDir()
	for _, name := range []string{"d", "b", "c"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte(name), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink(filepath.Join(root, "gone"), filepath.Join(root, "a")); err != nil {
		t.Fatal(err)
	}

	bounded, err := filesystemOperation(filesystemRequest{Operation: "list-files", Path: root, Limit: 2})
	if err != nil {
		t.Fatal(err)
	}
	if !bounded.Truncated || len(bounded.Files) != 2 || bounded.Files[0].Name != "a" || bounded.Files[1].Name != "b" {
		t.Fatalf("bounded listing returned %+v, truncated=%v", bounded.Files, bounded.Truncated)
	}

	full, err := filesystemOperation(filesystemRequest{Operation: "list-files", Path: root})
	if err != nil {
		t.Fatal(err)
	}
	if len(full.Files) != 4 || full.Truncated {
		t.Fatalf("unbounded listing returned %d files, truncated=%v", len(full.Files), full.Truncated)
	}
}

func TestDownloadRefusesOverItsLimitOrReturnsAPrefix(t *testing.T) {
	path := filepath.Join(t.TempDir(), "data")
	if err := os.WriteFile(path, []byte("12345"), 0o600); err != nil {
		t.Fatal(err)
	}

	if out, err := download(filesystemRequest{Operation: "download-file", Path: path, Limit: 5}); err != nil || out != "12345" {
		t.Fatalf("a file at the limit returned %q, %v", out, err)
	}
	if _, err := download(filesystemRequest{Operation: "download-file", Path: path, Limit: 4}); !errors.Is(err, errOverLimit) {
		t.Fatalf("a file over the limit returned %v", err)
	}
	if out, err := download(filesystemRequest{Operation: "download-file", Path: path, Limit: 3, Truncate: true}); err != nil || out != "123" {
		t.Fatalf("a prefix read returned %q, %v", out, err)
	}
	if err := runFilesystem(`{"operation":"download-file","path":"` + path + `","limit":` + "9223372036854775807" + `}`); err == nil || errors.Is(err, errOverLimit) {
		t.Fatalf("a limit of %d was accepted: %v", int64(math.MaxInt64), err)
	}
}

func download(request filesystemRequest) (string, error) {
	var output bytes.Buffer
	_, err := downloadFile(request, &output)
	return output.String(), err
}
