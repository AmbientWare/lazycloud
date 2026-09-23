package main

import (
	"bytes"
	"math/rand/v2"
	"testing"
)

func chunkBytes(t *testing.T, data []byte) []chunkSpan {
	t.Helper()
	var spans []chunkSpan
	err := chunkStream(bytes.NewReader(data), 0, func(span chunkSpan) error {
		spans = append(spans, span)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return spans
}

func TestInsertionKeepsLaterChunks(t *testing.T) {
	data := make([]byte, 96<<20)
	source := rand.NewChaCha8([32]byte{7})
	source.Read(data)

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
