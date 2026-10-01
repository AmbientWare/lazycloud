package diskengine

import (
	"bytes"
	"encoding/hex"
	"math/rand/v2"
	"testing"
)

func chunkBytes(t *testing.T, data []byte) []chunkSpan {
	t.Helper()
	var spans []chunkSpan
	err := newChunker().split(bytes.NewReader(data), 0, func(span chunkSpan) error {
		spans = append(spans, span)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return spans
}

func randomBytes(size int, seed byte) []byte {
	data := make([]byte, size)
	_, _ = rand.NewChaCha8([32]byte{seed}).Read(data)
	return data
}

func TestInsertionKeepsLaterChunks(t *testing.T) {
	data := randomBytes(96<<20, 7)
	before := chunkBytes(t, data)
	edited := append(append(append([]byte{}, data[:4096]...), bytes.Repeat([]byte{0x5a}, 777)...), data[4096:]...)
	after := chunkBytes(t, edited)

	covered := int64(0)
	for i, span := range before {
		if span.Length > maxChunkBytes || (i < len(before)-1 && span.Length < minChunkBytes) {
			t.Fatalf("chunk %d is %d bytes, outside [%d, %d]", i, span.Length, minChunkBytes, maxChunkBytes)
		}
		covered += span.Length
	}
	if covered != int64(len(data)) {
		t.Fatalf("chunks cover %d of %d bytes", covered, len(data))
	}

	shifted := map[[32]byte]int64{}
	for _, span := range after {
		shifted[span.Sum] = span.Offset
	}
	kept := 0
	for _, span := range before[2:] {
		offset, ok := shifted[span.Sum]
		if !ok {
			t.Fatalf("chunk at %d changed although the edit was at 4096", span.Offset)
		}
		if offset != span.Offset+777 {
			t.Fatalf("chunk at %d moved to %d, expected %d", span.Offset, offset, span.Offset+777)
		}
		kept++
	}
	if kept < 10 {
		t.Fatalf("only %d chunks to compare; the average chunk size is off", kept)
	}
}

// Stored chunks are named by these boundaries. A change to the gear table or
// the boundary rule makes every disk re-upload in full, so the cuts for a
// fixed input are pinned.
func TestChunkBoundariesArePinned(t *testing.T) {
	data := randomBytes(32<<20, 7)
	copy(data[12<<20:], make([]byte, 9<<20))
	type cut struct {
		offset, length int64
		zero           bool
		sum            string
	}
	var got []cut
	for _, span := range chunkBytes(t, data) {
		got = append(got, cut{span.Offset, span.Length, span.Zero, hex.EncodeToString(span.Sum[:])})
	}
	want := []cut{
		{0, 3656616, false, "4e9ea81351dd5b18f98db1ecaeeff6d41c3747141b1167ef1ee189eda6828e7a"},
		{3656616, 1397049, false, "7f520af840ba831ae475134e5386118754113b5585ecc4660b57ea714377a288"},
		{5053665, 3935589, false, "06f8232f6c922b996bd0cf7832322b21e93ad48711fcd3e751f8dcdad23e1a69"},
		{8989254, 8388608, false, "05597e5e93db8ccbf719d968f42660155e6bcceb713adc250a6e6b52ec50e85b"},
		{17377862, 4817849, false, "4a216738c28954293d78eb9289b1c99ea7d8979b23e96aae0be81c25d606e641"},
		{22195711, 3151010, false, "48e5e31fc52304c0742cdfab369fe5126e63218e2d837bf2e5147dba739c2cad"},
		{25346721, 5529526, false, "2088a8381b6194899230860b148741c1a252955d2b94132468d4f82ef8fb4dac"},
		{30876247, 2678185, false, "b6e9544201f064338af5a3a8ac1e7003b4f741c07e45a6209b9a329a9a104d48"},
	}
	if len(got) != len(want) {
		t.Fatalf("got %d chunks %+v, want %d", len(got), got, len(want))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("chunk %d is %+v, want %+v", i, got[i], want[i])
		}
	}
}
