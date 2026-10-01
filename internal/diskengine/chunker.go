package diskengine

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
)

// Content-defined chunk bounds. A boundary depends only on the 64 bytes before
// it, so an insertion or deletion moves only the boundaries near it. A
// flattened layer, or a layer whose clusters moved, re-finds the chunks the
// disk already stores.
const (
	minChunkBytes = 1 << 20
	avgChunkBytes = 4 << 20
	maxChunkBytes = 8 << 20
	gearWindow    = 64
)

// Below the average a boundary needs 22 zero bits, above it 21, which pulls
// sizes towards the average and leaves few chunks at the maximum. Masks test
// the high bits, since a shift-left hash makes those depend on all 64 window
// bytes.
const (
	strictMask = uint64(1<<22-1) << (64 - 22)
	looseMask  = uint64(1<<21-1) << (64 - 21)
)

// chunker splits layers into chunks. Its gear table decides every boundary.
// Changing the table stops new layers from matching stored chunks, so the
// next publish re-uploads whole disks.
type chunker struct {
	gear [256]uint64
}

func newChunker() *chunker {
	c := &chunker{}
	for i := range c.gear {
		sum := sha256.Sum256(fmt.Appendf(nil, "lazycloud disk chunk gear %d", i))
		c.gear[i] = binary.LittleEndian.Uint64(sum[:8])
	}
	return c
}

type boundaryFinder struct {
	gear   *[256]uint64
	hash   uint64
	length int
}

// next consumes p up to the end of the current chunk and returns how many
// bytes that took, or -1 when the chunk continues past p.
func (b *boundaryFinder) next(p []byte) int {
	i := 0
	// Bytes more than a window before the minimum cannot influence any hash
	// that is tested, so they are skipped rather than hashed.
	if skip := minChunkBytes - gearWindow - b.length; skip > 0 {
		if skip >= len(p) {
			b.length += len(p)
			return -1
		}
		i = skip
		b.length += skip
	}
	hash := b.hash
	for ; i < len(p); i++ {
		hash = hash<<1 + b.gear[p[i]]
		b.length++
		if b.length < minChunkBytes {
			continue
		}
		mask := looseMask
		if b.length < avgChunkBytes {
			mask = strictMask
		}
		if hash&mask == 0 || b.length >= maxChunkBytes {
			b.hash, b.length = 0, 0
			return i + 1
		}
	}
	b.hash = hash
	return -1
}

type chunkSpan struct {
	Offset int64
	Length int64
	Sum    [32]byte
	Zero   bool
}

// isZero reports whether p holds only zero bytes: its first byte is zero and
// every byte equals the one before it.
func isZero(p []byte) bool {
	return len(p) == 0 || (p[0] == 0 && bytes.Equal(p[1:], p[:len(p)-1]))
}

// split cuts r into content-defined chunks whose offsets start at base.
func (c *chunker) split(r io.Reader, base int64, emit func(chunkSpan) error) error {
	buffer := make([]byte, 1<<20)
	finder := boundaryFinder{gear: &c.gear}
	hasher := sha256.New()
	span := chunkSpan{Offset: base, Zero: true}
	flush := func() error {
		if span.Length == 0 {
			return nil
		}
		hasher.Sum(span.Sum[:0])
		hasher.Reset()
		done := span
		span = chunkSpan{Offset: span.Offset + span.Length, Zero: true}
		return emit(done)
	}
	for {
		n, readErr := io.ReadFull(r, buffer)
		p := buffer[:n]
		for len(p) > 0 {
			cut := finder.next(p)
			take := len(p)
			if cut >= 0 {
				take = cut
			}
			hasher.Write(p[:take])
			if span.Zero && !isZero(p[:take]) {
				span.Zero = false
			}
			span.Length += int64(take)
			p = p[take:]
			if cut >= 0 {
				if err := flush(); err != nil {
					return err
				}
			}
		}
		if errors.Is(readErr, io.EOF) || errors.Is(readErr, io.ErrUnexpectedEOF) {
			return flush()
		}
		if readErr != nil {
			return fmt.Errorf("read layer: %w", readErr)
		}
	}
}
