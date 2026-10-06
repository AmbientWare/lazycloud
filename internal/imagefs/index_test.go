package imagefs

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io/fs"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/klauspost/compress/zstd"
	"google.golang.org/protobuf/encoding/protowire"

	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
)

func testIndex() Index {
	return Index{
		Layer:      Digest("sha256:" + strings.Repeat("ab", 32)),
		StreamSize: FrameSize + 10,
		DataSize:   300,
		Frames:     []Frame{{Offset: 0, Size: 200}, {Offset: 200, Size: 100}},
		Entries: []Entry{
			{Path: "app", Type: TypeDirectory, Mode: fs.ModeDir | 0o755, ModTime: time.Unix(0, 0).UTC()},
			{Path: "app/a", Type: TypeRegular, Mode: 0o644, ModTime: time.Unix(0, 0).UTC(), Offset: FrameSize, Size: 10},
			{Path: "app/b", Type: TypeHardLink, Mode: 0o644, ModTime: time.Unix(0, 0).UTC(), LinkTarget: "app/a", Offset: FrameSize, Size: 10},
		},
	}
}

// A reader refuses an index of another format version, or one whose tree
// or frame table it cannot trust, rather than serving wrong bytes.
func TestUnmarshalRefusesUnknownVersionsAndBrokenIndexes(t *testing.T) {
	stored, err := testIndex().Marshal()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Unmarshal(stored); err != nil {
		t.Fatalf("the valid index: %v", err)
	}
	next := append([]byte(nil), stored...)
	binary.BigEndian.PutUint32(next[len(indexMagic):], formatVersion+1)
	if _, err := Unmarshal(next); !errors.Is(err, ErrUnsupportedVersion) {
		t.Fatalf("version %d: %v", formatVersion+1, err)
	}

	broken := map[string]func(*Index){
		"a child before its directory": func(ix *Index) { ix.Entries[0], ix.Entries[1] = ix.Entries[1], ix.Entries[0] },
		"bytes past the stream":        func(ix *Index) { ix.Entries[1].Size = 11 },
		"a frame missing":              func(ix *Index) { ix.Frames, ix.DataSize = ix.Frames[:1], 200 },
		"a path that is not clean":     func(ix *Index) { ix.Entries[1].Path = "app/../a" },
		"a dangling hard link":         func(ix *Index) { ix.Entries[2].LinkTarget = "app/c" },
		"a hard link of other bytes":   func(ix *Index) { ix.Entries[2].Size = 9 },
		"a hard link to a whiteout": func(ix *Index) {
			ix.Entries[1] = Entry{Path: "app/a", Type: TypeCharDevice, Whiteout: true}
			ix.Entries[2].Offset, ix.Entries[2].Size = 0, 0
		},
	}
	for name, breakIt := range broken {
		ix := testIndex()
		breakIt(&ix)
		stored, err := ix.Marshal()
		if err != nil {
			t.Fatal(err)
		}
		if _, err := Unmarshal(stored); !errors.Is(err, ErrInvalidIndex) {
			t.Errorf("%s: %v", name, err)
		}
	}
	if _, err := Unmarshal(stored[:headerSize+3]); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("a truncated index: %v", err)
	}
}

// An index of millions of empty records is refused before any of them is
// allocated, so a few kilobytes cannot take gigabytes to decode.
func TestUnmarshalCountsRecordsBeforeDecoding(t *testing.T) {
	fields := (&imagefsproto.Index{}).ProtoReflect().Descriptor().Fields()
	entries := protowire.AppendTag(nil, fields.ByName("entries").Number(), protowire.BytesType)
	entries = protowire.AppendBytes(entries, nil)
	frames := bytes.Repeat([]byte{1}, 32<<20) // one-byte varints
	records := map[string][]byte{
		"entries": bytes.Repeat(entries, maxEntries+1),
		"frames":  protowire.AppendBytes(protowire.AppendTag(nil, fields.ByName("frame_sizes").Number(), protowire.BytesType), frames),
	}
	enc, err := zstd.NewWriter(nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = enc.Close() }()
	for name, raw := range records {
		stored := binary.BigEndian.AppendUint32([]byte(indexMagic), formatVersion)
		stored = enc.EncodeAll(raw, stored)
		var before, after runtime.MemStats
		runtime.ReadMemStats(&before)
		_, err := Unmarshal(stored)
		runtime.ReadMemStats(&after)
		if !errors.Is(err, ErrInvalidIndex) {
			t.Fatalf("%s: %v", name, err)
		}
		if allocated := after.TotalAlloc - before.TotalAlloc; allocated > 3*uint64(len(raw))+16<<20 {
			t.Errorf("%s: a %d byte index of %d decoded bytes allocated %d bytes", name, len(stored), len(raw), allocated)
		}
	}
}
