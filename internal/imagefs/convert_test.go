package imagefs

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"
)

// serve is a RangeReader of object, served over HTTP.
func serve(t *testing.T, object []byte) RangeReader {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.ServeContent(w, r, "", time.Time{}, bytes.NewReader(object))
	}))
	t.Cleanup(server.Close)
	return HTTPObject(server.Client(), func() string { return server.URL })
}

// roundTrip converts a layer and returns the index as a reader decodes it,
// and the data object.
func roundTrip(t *testing.T, layer []byte) (Index, []byte) {
	t.Helper()
	var data bytes.Buffer
	ix, err := Convert(t.Context(), bytes.NewReader(layer), &data)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(layer)
	if want := Digest("sha256:" + hex.EncodeToString(sum[:])); ix.Layer != want {
		t.Fatalf("layer digest %s, want %s", ix.Layer, want)
	}
	if ix.DataSize != int64(data.Len()) {
		t.Fatalf("index says %d data bytes, wrote %d", ix.DataSize, data.Len())
	}
	stored, err := ix.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	got, err := Unmarshal(stored)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, ix) {
		t.Fatal("the decoded index differs from the converted one")
	}
	return got, data.Bytes()
}

// contents reads e's bytes frame by frame, keeping decoded frames in frames.
func contents(t *testing.T, ix Index, data RangeReader, frames map[int][]byte, e Entry) []byte {
	t.Helper()
	if e.Size == 0 {
		return nil
	}
	first, last := e.Offset/FrameSize, (e.Offset+e.Size-1)/FrameSize
	if e.Size <= FrameSize && first != last {
		t.Fatalf("%s of %d bytes spans frames %d-%d", e.Path, e.Size, first, last)
	}
	var out []byte
	for i := int(first); i <= int(last); i++ {
		frame, ok := frames[i]
		if !ok {
			var err error
			if frame, err = ix.ReadFrame(t.Context(), data, i); err != nil {
				t.Fatal(err)
			}
			frames[i] = frame
		}
		start := max(e.Offset-int64(i)*FrameSize, 0)
		end := min(e.Offset+e.Size-int64(i)*FrameSize, int64(len(frame)))
		out = append(out, frame[start:end]...)
	}
	return out
}

// checkAgainstTar compares every tar member with its entry, field by field
// and byte for byte.
func checkAgainstTar(t *testing.T, layer []byte, ix Index, data RangeReader) {
	t.Helper()
	byPath := map[string]Entry{}
	for _, e := range ix.Entries {
		byPath[e.Path] = e
	}
	frames := map[int][]byte{}
	members := map[string]bool{}
	tr := tar.NewReader(bytes.NewReader(layer))
	for {
		hdr, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		p := cleanPath(hdr.Name)
		if strings.HasPrefix(path.Base(p), whiteoutPrefix) {
			t.Fatalf("real layer holds whiteout %s; extend the check", p)
		}
		members[p] = true
		e, ok := byPath[p]
		if !ok {
			t.Fatalf("no entry for %s", p)
		}
		info := hdr.FileInfo()
		if e.Mode.Perm() != info.Mode().Perm() || e.Mode&(fs.ModeSetuid|fs.ModeSetgid|fs.ModeSticky) != info.Mode()&(fs.ModeSetuid|fs.ModeSetgid|fs.ModeSticky) ||
			e.UID != uint32(hdr.Uid) || e.GID != uint32(hdr.Gid) || !e.ModTime.Equal(hdr.ModTime) ||
			e.DevMajor != uint32(hdr.Devmajor) || e.DevMinor != uint32(hdr.Devminor) {
			t.Fatalf("%s: entry %+v, tar header %+v", p, e, hdr)
		}
		for k, v := range hdr.PAXRecords {
			if name, ok := strings.CutPrefix(k, xattrPrefix); ok && string(e.Xattrs[name]) != v {
				t.Fatalf("%s: xattr %s is %q, want %q", p, name, e.Xattrs[name], v)
			}
		}
		want := map[byte]EntryType{
			tar.TypeReg: TypeRegular, tar.TypeDir: TypeDirectory, tar.TypeSymlink: TypeSymlink, tar.TypeLink: TypeHardLink,
			tar.TypeChar: TypeCharDevice, tar.TypeBlock: TypeBlockDevice, tar.TypeFifo: TypeFIFO,
		}[hdr.Typeflag]
		if e.Type != want {
			t.Fatalf("%s: type %s, want %s", p, e.Type, want)
		}
		switch e.Type {
		case TypeRegular:
			body, err := io.ReadAll(tr)
			if err != nil {
				t.Fatal(err)
			}
			if got := contents(t, ix, data, frames, e); !bytes.Equal(got, body) {
				t.Fatalf("%s: %d bytes read back differ from the tar's %d", p, len(got), len(body))
			}
		case TypeHardLink:
			target := byPath[e.LinkTarget]
			if target.Type == TypeHardLink || e.Offset != target.Offset || e.Size != target.Size {
				t.Fatalf("%s: hard link %+v to %+v", p, e, target)
			}
		case TypeSymlink:
			if e.LinkTarget != hdr.Linkname {
				t.Fatalf("%s: symlink to %q, want %q", p, e.LinkTarget, hdr.Linkname)
			}
		case TypeDirectory, TypeCharDevice, TypeBlockDevice, TypeFIFO:
		}
	}
	for _, e := range ix.Entries {
		if !members[e.Path] && (e.Type != TypeDirectory || e.Mode != fs.ModeDir|0o755) {
			t.Fatalf("entry %+v is neither in the tar nor an added parent directory", e)
		}
	}
}

// Every layer of python:3.12-slim reads back as the tar holds it, and its
// index names the layer by the image's diff_id.
func TestPythonLayersRoundTrip(t *testing.T) {
	const image = "python:3.12-slim"
	dir := t.TempDir()
	if err := exec.CommandContext(t.Context(), "docker", "image", "inspect", image).Run(); err != nil {
		if out, err := exec.CommandContext(t.Context(), "docker", "pull", image).CombinedOutput(); err != nil {
			t.Fatalf("docker pull: %v: %s", err, out)
		}
	}
	saved := filepath.Join(dir, "image.tar")
	if out, err := exec.CommandContext(t.Context(), "docker", "save", "-o", saved, image).CombinedOutput(); err != nil {
		t.Fatalf("docker save: %v: %s", err, out)
	}
	blobs := readTar(t, saved)
	var manifest []struct {
		Config string
		Layers []string
	}
	if err := json.Unmarshal(blobs["manifest.json"], &manifest); err != nil || len(manifest) != 1 {
		t.Fatalf("manifest: %v", err)
	}
	var config struct {
		RootFS struct {
			DiffIDs []Digest `json:"diff_ids"`
		} `json:"rootfs"`
	}
	if err := json.Unmarshal(blobs[manifest[0].Config], &config); err != nil {
		t.Fatal(err)
	}
	layers := manifest[0].Layers
	if len(layers) != len(config.RootFS.DiffIDs) || len(layers) < 2 {
		t.Fatalf("%d layers for %d diff_ids", len(layers), len(config.RootFS.DiffIDs))
	}
	for i, name := range layers {
		layer := blobs[name]
		if bytes.HasPrefix(layer, []byte{0x1f, 0x8b}) {
			zr, err := gzip.NewReader(bytes.NewReader(layer))
			if err != nil {
				t.Fatal(err)
			}
			if layer, err = io.ReadAll(zr); err != nil {
				t.Fatal(err)
			}
		}
		ix, data := roundTrip(t, layer)
		if ix.Layer != config.RootFS.DiffIDs[i] {
			t.Fatalf("layer %d digest %s, image diff_id %s", i, ix.Layer, config.RootFS.DiffIDs[i])
		}
		checkAgainstTar(t, layer, ix, serve(t, data))
		t.Logf("layer %d: %d bytes tar, %d entries, %d frames, %d data bytes", i, len(layer), len(ix.Entries), len(ix.Frames), ix.DataSize)
	}
}

func readTar(t *testing.T, file string) map[string][]byte {
	t.Helper()
	f, err := os.Open(file)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = f.Close() }()
	out := map[string][]byte{}
	tr := tar.NewReader(f)
	for {
		hdr, err := tr.Next()
		if errors.Is(err, io.EOF) {
			return out
		}
		if err != nil {
			t.Fatal(err)
		}
		if hdr.Typeflag == tar.TypeReg {
			if out[hdr.Name], err = io.ReadAll(tr); err != nil {
				t.Fatal(err)
			}
		}
	}
}

// A layer with every entry kind converts to the tree extraction would
// build: whiteouts and opaque markers become overlayfs markers, hard links
// share their target's bytes, later members replace earlier ones and
// missing parents appear.
func TestConvertKeepsEveryEntryKind(t *testing.T) {
	mtime := time.Date(2026, 10, 5, 12, 30, 0, 0, time.UTC)
	epoch := time.Unix(0, 0).UTC()
	tool := make([]byte, 2*FrameSize+123)
	_, _ = rand.Read(tool)
	big := bytes.Repeat([]byte("lazycloud "), (FrameSize-100)/10)
	var layer bytes.Buffer
	tw := tar.NewWriter(&layer)
	add := func(hdr tar.Header, body []byte) {
		hdr.ModTime, hdr.Format, hdr.Size = mtime, tar.FormatPAX, int64(len(body))
		if hdr.Mode == 0 {
			hdr.Mode = 0o644
		}
		if err := tw.WriteHeader(&hdr); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write(body); err != nil {
			t.Fatal(err)
		}
	}
	add(tar.Header{Name: "./", Typeflag: tar.TypeDir, Mode: 0o755}, nil)
	add(tar.Header{Name: "etc/", Typeflag: tar.TypeDir, Mode: 0o755, PAXRecords: map[string]string{
		"SCHILY.xattr.user.color": "blue", "SCHILY.xattr.security.capability": "\x01\x00\x00\x02",
	}}, nil)
	add(tar.Header{Name: "etc/.wh..wh..opq", Typeflag: tar.TypeReg}, nil)
	add(tar.Header{Name: "etc/hosts", Typeflag: tar.TypeReg, Uid: 1000, Gid: 1000}, []byte("first\n"))
	add(tar.Header{Name: "/bin/../usr/bin/tool", Typeflag: tar.TypeReg, Mode: 0o4755}, tool)
	add(tar.Header{Name: "usr/bin/tool-link", Typeflag: tar.TypeLink, Linkname: "usr/bin/tool", Mode: 0o4755}, nil)
	add(tar.Header{Name: "usr/bin/tool-link2", Typeflag: tar.TypeLink, Linkname: "./usr/bin/tool-link", Mode: 0o4755}, nil)
	add(tar.Header{Name: "usr/bin/sh", Typeflag: tar.TypeSymlink, Linkname: "../../bin/busybox", Mode: 0o777}, nil)
	add(tar.Header{Name: "dev/", Typeflag: tar.TypeDir, Mode: 0o1777}, nil)
	add(tar.Header{Name: "dev/null", Typeflag: tar.TypeChar, Devmajor: 1, Devminor: 3, Mode: 0o666}, nil)
	add(tar.Header{Name: "dev/sda", Typeflag: tar.TypeBlock, Devmajor: 8, Mode: 0o660}, nil)
	add(tar.Header{Name: "dev/fifo", Typeflag: tar.TypeFifo, Mode: 0o600}, nil)
	add(tar.Header{Name: "var/.wh.cache", Typeflag: tar.TypeReg, Mode: 0o600}, nil)
	add(tar.Header{Name: ".wh..wh.plnk/", Typeflag: tar.TypeDir, Mode: 0o700}, nil)
	add(tar.Header{Name: "big", Typeflag: tar.TypeReg}, big)
	add(tar.Header{Name: "empty", Typeflag: tar.TypeReg}, nil)
	add(tar.Header{Name: "etc/hosts", Typeflag: tar.TypeReg, Uid: 1000, Gid: 1000}, []byte("second\n"))
	add(tar.Header{Name: "gone/", Typeflag: tar.TypeDir, Mode: 0o755}, nil)
	add(tar.Header{Name: "gone/child", Typeflag: tar.TypeReg}, []byte("child"))
	add(tar.Header{Name: "gone", Typeflag: tar.TypeReg}, []byte("now a file"))
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}

	ix, stored := roundTrip(t, layer.Bytes())
	object := serve(t, stored)
	toolSize := int64(len(tool))
	// big would cross the frame boundary after tool, so it starts the next
	// frame; the later members follow it.
	bigOffset := int64(3 * FrameSize)
	hostsOffset := bigOffset + int64(len(big))
	gone := hostsOffset + int64(len("second\n")) + int64(len("child"))
	want := []Entry{
		{Path: ".", Type: TypeDirectory, Mode: fs.ModeDir | 0o755, ModTime: mtime},
		{Path: "etc", Type: TypeDirectory, Mode: fs.ModeDir | 0o755, ModTime: mtime, Opaque: true, Xattrs: map[string][]byte{
			"user.color": []byte("blue"), "security.capability": {1, 0, 0, 2},
		}},
		{Path: "etc/hosts", Type: TypeRegular, Mode: 0o644, UID: 1000, GID: 1000, ModTime: mtime, Offset: hostsOffset, Size: 7},
		{Path: "usr", Type: TypeDirectory, Mode: fs.ModeDir | 0o755, ModTime: epoch},
		{Path: "usr/bin", Type: TypeDirectory, Mode: fs.ModeDir | 0o755, ModTime: epoch},
		{Path: "usr/bin/tool", Type: TypeRegular, Mode: fs.ModeSetuid | 0o755, ModTime: mtime, Offset: 6, Size: toolSize},
		{Path: "usr/bin/tool-link", Type: TypeHardLink, Mode: fs.ModeSetuid | 0o755, ModTime: mtime, LinkTarget: "usr/bin/tool", Offset: 6, Size: toolSize},
		{Path: "usr/bin/tool-link2", Type: TypeHardLink, Mode: fs.ModeSetuid | 0o755, ModTime: mtime, LinkTarget: "usr/bin/tool", Offset: 6, Size: toolSize},
		{Path: "usr/bin/sh", Type: TypeSymlink, Mode: fs.ModeSymlink | 0o777, ModTime: mtime, LinkTarget: "../../bin/busybox"},
		{Path: "dev", Type: TypeDirectory, Mode: fs.ModeDir | fs.ModeSticky | 0o777, ModTime: mtime},
		{Path: "dev/null", Type: TypeCharDevice, Mode: fs.ModeDevice | fs.ModeCharDevice | 0o666, ModTime: mtime, DevMajor: 1, DevMinor: 3},
		{Path: "dev/sda", Type: TypeBlockDevice, Mode: fs.ModeDevice | 0o660, ModTime: mtime, DevMajor: 8},
		{Path: "dev/fifo", Type: TypeFIFO, Mode: fs.ModeNamedPipe | 0o600, ModTime: mtime},
		{Path: "var", Type: TypeDirectory, Mode: fs.ModeDir | 0o755, ModTime: epoch},
		{Path: "var/cache", Type: TypeCharDevice, Mode: fs.ModeDevice | fs.ModeCharDevice | 0o600, ModTime: mtime, Whiteout: true},
		{Path: "big", Type: TypeRegular, Mode: 0o644, ModTime: mtime, Offset: bigOffset, Size: int64(len(big))},
		{Path: "empty", Type: TypeRegular, Mode: 0o644, ModTime: mtime},
		{Path: "gone", Type: TypeRegular, Mode: 0o644, ModTime: mtime, Offset: gone, Size: int64(len("now a file"))},
	}
	if !reflect.DeepEqual(ix.Entries, want) {
		for i := range max(len(ix.Entries), len(want)) {
			var got, w Entry
			if i < len(ix.Entries) {
				got = ix.Entries[i]
			}
			if i < len(want) {
				w = want[i]
			}
			if !reflect.DeepEqual(got, w) {
				t.Errorf("entry %d:\n got %+v\nwant %+v", i, got, w)
			}
		}
		t.FailNow()
	}
	frames := map[int][]byte{}
	for path, body := range map[string][]byte{
		"etc/hosts": []byte("second\n"), "usr/bin/tool": tool, "usr/bin/tool-link2": tool, "big": big, "empty": nil, "gone": []byte("now a file"),
	} {
		e := ix.Entries[slices.IndexFunc(ix.Entries, func(e Entry) bool { return e.Path == path })]
		if got := contents(t, ix, object, frames, e); !bytes.Equal(got, body) {
			t.Fatalf("%s reads back %d bytes, want %d", path, len(got), len(body))
		}
	}
}

// Convert refuses a layer it cannot index faithfully rather than storing a
// tree that differs from extraction.
func TestConvertRefusesLayersItCannotIndex(t *testing.T) {
	layers := map[string][]tar.Header{
		"a hard link to a missing file": {{Name: "b", Typeflag: tar.TypeLink, Linkname: "a"}},
		"a hard link target replaced": {
			{Name: "a", Typeflag: tar.TypeReg}, {Name: "b", Typeflag: tar.TypeLink, Linkname: "a"}, {Name: "a", Typeflag: tar.TypeReg},
		},
		"a file below a file": {{Name: "a", Typeflag: tar.TypeReg}, {Name: "a/b", Typeflag: tar.TypeReg}},
	}
	for name, headers := range layers {
		var layer bytes.Buffer
		tw := tar.NewWriter(&layer)
		for _, hdr := range headers {
			hdr.Mode = 0o644
			if err := tw.WriteHeader(&hdr); err != nil {
				t.Fatal(err)
			}
		}
		if err := tw.Close(); err != nil {
			t.Fatal(err)
		}
		if _, err := Convert(t.Context(), &layer, io.Discard); !errors.Is(err, ErrInvalidLayer) {
			t.Errorf("%s: %v", name, err)
		}
	}
	var truncated bytes.Buffer
	tw := tar.NewWriter(&truncated)
	if err := tw.WriteHeader(&tar.Header{Name: "a", Typeflag: tar.TypeReg, Mode: 0o644, Size: 1000}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write(make([]byte, 100)); err != nil {
		t.Fatal(err)
	}
	if _, err := Convert(t.Context(), &truncated, io.Discard); !errors.Is(err, ErrInvalidLayer) {
		t.Errorf("a truncated layer: %v", err)
	}
	// A stream cut inside a header, and a header that is not one.
	var whole bytes.Buffer
	tw = tar.NewWriter(&whole)
	if err := tw.WriteHeader(&tar.Header{Name: "a", Typeflag: tar.TypeReg, Mode: 0o644}); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	streams := map[string][]byte{
		"a stream cut inside a header": whole.Bytes()[:100],
		"a malformed header":           bytes.Repeat([]byte("not a tar header"), 32),
	}
	for name, stream := range streams {
		if _, err := Convert(t.Context(), bytes.NewReader(stream), io.Discard); !errors.Is(err, ErrInvalidLayer) {
			t.Errorf("%s: %v", name, err)
		}
	}
}

// A whiteout hides only the lower layers, in either order against an entry
// of the same layer: a file stays, and a directory stays and turns opaque.
func TestWhiteoutsHideOnlyLowerLayers(t *testing.T) {
	mtime := time.Date(2026, 10, 5, 12, 30, 0, 0, time.UTC)
	var layer bytes.Buffer
	tw := tar.NewWriter(&layer)
	for _, hdr := range []tar.Header{
		{Name: "file-first", Typeflag: tar.TypeReg},
		{Name: ".wh.file-first", Typeflag: tar.TypeReg},
		{Name: "dir-first/", Typeflag: tar.TypeDir},
		{Name: ".wh.dir-first", Typeflag: tar.TypeReg},
		{Name: ".wh.file-later", Typeflag: tar.TypeReg},
		{Name: "file-later", Typeflag: tar.TypeReg},
		{Name: ".wh.dir-later", Typeflag: tar.TypeReg},
		{Name: "dir-later/", Typeflag: tar.TypeDir},
		{Name: ".wh.parent", Typeflag: tar.TypeReg},
		{Name: "parent/child", Typeflag: tar.TypeReg},
	} {
		hdr.Mode, hdr.ModTime = 0o755, mtime
		if err := tw.WriteHeader(&hdr); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	ix, _ := roundTrip(t, layer.Bytes())
	want := []Entry{
		{Path: "file-first", Type: TypeRegular, Mode: 0o755, ModTime: mtime},
		{Path: "dir-first", Type: TypeDirectory, Mode: fs.ModeDir | 0o755, ModTime: mtime, Opaque: true},
		{Path: "file-later", Type: TypeRegular, Mode: 0o755, ModTime: mtime},
		{Path: "dir-later", Type: TypeDirectory, Mode: fs.ModeDir | 0o755, ModTime: mtime, Opaque: true},
		{Path: "parent", Type: TypeDirectory, Mode: fs.ModeDir | 0o755, ModTime: time.Unix(0, 0).UTC(), Opaque: true},
		{Path: "parent/child", Type: TypeRegular, Mode: 0o755, ModTime: mtime},
	}
	if !reflect.DeepEqual(ix.Entries, want) {
		t.Fatalf("entries:\n got %+v\nwant %+v", ix.Entries, want)
	}
}

// An old GNU sparse member reads back with its holes filled, as extraction
// writes it.
func TestConvertFillsSparseFiles(t *testing.T) {
	data := bytes.Repeat([]byte("x"), 1000)
	hdr := make([]byte, 512)
	octal := func(off, n int, v int64) { copy(hdr[off:off+n-1], fmt.Sprintf("%0*o", n-1, v)) }
	copy(hdr, "sparse")
	octal(100, 8, 0o644)
	octal(108, 8, 0)
	octal(116, 8, 0)
	octal(124, 12, int64(len(data)))
	octal(136, 12, 0)
	hdr[156] = tar.TypeGNUSparse
	copy(hdr[257:], "ustar  \x00")
	octal(386, 12, 8192)             // the one data region's offset
	octal(398, 12, int64(len(data))) // and length
	octal(483, 12, 8192+1000+808)    // the file's full size
	copy(hdr[148:156], "        ")
	var sum int64
	for _, b := range hdr {
		sum += int64(b)
	}
	copy(hdr[148:], fmt.Sprintf("%06o\x00 ", sum))
	layer := append(hdr, data...)
	layer = append(layer, make([]byte, 24+1024)...) // block padding and the end of archive

	ix, object := roundTrip(t, layer)
	want := append(make([]byte, 8192), data...)
	want = append(want, make([]byte, 808)...)
	if len(ix.Entries) != 1 || ix.Entries[0].Type != TypeRegular {
		t.Fatalf("entries %+v", ix.Entries)
	}
	if got := contents(t, ix, serve(t, object), map[int][]byte{}, ix.Entries[0]); !bytes.Equal(got, want) {
		t.Fatalf("read back %d bytes, want the %d of the filled file", len(got), len(want))
	}
}

// Every frame carries zstd's content checksum, so a frame changed in the
// store or a cache is an error, never wrong bytes.
func TestReadFrameRefusesCorruptFrames(t *testing.T) {
	body := make([]byte, FrameSize)
	_, _ = rand.Read(body)
	ix, data := roundTrip(t, fileTar(t, "weights", body))
	for _, at := range []int{len(data) / 3, len(data) / 2, len(data) - 100} {
		corrupt := append([]byte(nil), data...)
		corrupt[at] ^= 0x40
		if got, err := ix.ReadFrame(t.Context(), serve(t, corrupt), 0); !errors.Is(err, ErrInvalidIndex) {
			t.Fatalf("a frame changed at byte %d: %d bytes, %v", at, len(got), err)
		}
	}
}

// fileTar is a layer of one regular file, after the headers before it.
func fileTar(t *testing.T, name string, body []byte, before ...tar.Header) []byte {
	t.Helper()
	var layer bytes.Buffer
	tw := tar.NewWriter(&layer)
	for _, hdr := range before {
		if err := tw.WriteHeader(&hdr); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.WriteHeader(&tar.Header{Name: name, Typeflag: tar.TypeReg, Mode: 0o644, Size: int64(len(body))}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write(body); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	return layer.Bytes()
}

// A PAX global header, which git archive and some image builders write,
// describes no file: conversion skips it as extraction does.
func TestConvertSkipsPAXGlobalHeaders(t *testing.T) {
	global := tar.Header{Typeflag: tar.TypeXGlobalHeader, Name: "pax_global_header", PAXRecords: map[string]string{"comment": "build 7"}}
	ix, _ := roundTrip(t, fileTar(t, "app", []byte("hi"), global))
	if len(ix.Entries) != 1 || ix.Entries[0].Path != "app" {
		t.Fatalf("entries %+v, want only app", ix.Entries)
	}
}

// ConvertFile keeps the data file of a layer it converts and leaves none
// behind for one it refuses, for its tar or for its diff_id.
func TestConvertFileLeavesNoFileOnFailure(t *testing.T) {
	layer := fileTar(t, "app", []byte("hi"))
	sum := sha256.Sum256(layer)
	diffID := Digest("sha256:" + hex.EncodeToString(sum[:]))
	dir := t.TempDir()
	refused := map[string]struct {
		layer  []byte
		diffID Digest
	}{
		"another diff_id": {layer, Digest("sha256:" + strings.Repeat("0", 64))},
		"a truncated tar": {layer[:600], diffID},
	}
	for name, r := range refused {
		if _, err := ConvertFile(t.Context(), bytes.NewReader(r.layer), dir, r.diffID); !errors.Is(err, ErrInvalidLayer) {
			t.Fatalf("%s: %v", name, err)
		}
		if left, _ := os.ReadDir(dir); len(left) != 0 {
			t.Fatalf("%s left %d files", name, len(left))
		}
	}
	f, err := ConvertFile(t.Context(), bytes.NewReader(layer), dir, diffID)
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(f.Path)
	if err != nil || int64(len(data)) != f.DataBytes || f.Entries != 1 {
		t.Fatalf("converted %+v, a file of %d bytes: %v", f, len(data), err)
	}
	if ix, err := Unmarshal(f.Index); err != nil || ix.Layer != diffID || ix.DataSize != f.DataBytes {
		t.Fatalf("index %+v: %v", ix, err)
	}
}
