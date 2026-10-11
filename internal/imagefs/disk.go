package imagefs

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"regexp"

	"github.com/klauspost/compress/zstd"
	"google.golang.org/protobuf/reflect/protoreflect"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
)

// maxDiskFrames bounds a disk's frame table.
const maxDiskFrames = hostproto.MaxDiskBytes / FrameSize

// DiskIndex is one published generation of a durable disk: frame i holds
// the disk's bytes [i*FrameSize, (i+1)*FrameSize). The schema is
// contracts/imagefs/v1/index.proto.
type DiskIndex struct {
	Size   int64
	Frames []DiskFrame
	// Start is the frames read in the minute after the latest attach, in
	// order; Recent the frames the cache held at the latest stop, most
	// recent first.
	Start, Recent []uint32
}

// DiskFrame is one frame of a disk: the sha256 of its bytes and the size of
// its stored copy, or a zero Size for a frame of zeros, stored nowhere.
type DiskFrame struct {
	Digest [sha256.Size]byte
	Size   int64
}

// Zero reports whether the frame holds only zeros.
func (f DiskFrame) Zero() bool { return f.Size == 0 }

// Name is where the frame is stored under its disk's prefix.
func (f DiskFrame) Name() string { return "frames/" + hex.EncodeToString(f.Digest[:]) }

var diskID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)

// DiskID reports whether id may name a disk's objects and local files: a
// letter or digit, then up to 127 letters, digits, underscores or hyphens.
func DiskID(id string) bool { return diskID.MatchString(id) }

// DiskPrefix holds every object of disk id in its workspace bucket.
func DiskPrefix(id string) string { return "disks/" + id + "/" }

// DiskIndexKey is where generation's index of disk id is stored, named by
// the sha256 of its bytes, so an upload never replaces a different index of
// the same generation, such as one a stale holder wrote after the disk
// changed hands.
func DiskIndexKey(id string, generation int64, sha256 string) string {
	return fmt.Sprintf("%smanifests/%012d-%s", DiskPrefix(id), generation, sha256)
}

// DiskFrames is how many frames a disk of size bytes has.
func DiskFrames(size int64) int { return int(ceilDiv(size, FrameSize)) }

// FrameLen is the length of frame i.
func (d DiskIndex) FrameLen(i int) int {
	return int(min(FrameSize, d.Size-int64(i)*FrameSize))
}

// Marshal encodes the index in the stored format.
func (d DiskIndex) Marshal() ([]byte, error) {
	if err := d.validate(); err != nil {
		return nil, fmt.Errorf("%w: %w", ErrInvalidIndex, err)
	}
	msg := &imagefsproto.DiskIndex{
		Size:         d.Size,
		FrameDigests: make([][]byte, len(d.Frames)),
		FrameSizes:   make([]int64, len(d.Frames)),
		StartFrames:  d.Start,
		RecentFrames: d.Recent,
	}
	for i, f := range d.Frames {
		if !f.Zero() {
			msg.FrameDigests[i] = f.Digest[:]
		}
		msg.FrameSizes[i] = f.Size
	}
	return marshalStored(diskIndexMagic, msg)
}

// UnmarshalDisk decodes a stored disk index, refusing one that breaks the
// format's invariants with ErrInvalidIndex.
func UnmarshalDisk(b []byte) (DiskIndex, error) {
	var msg imagefsproto.DiskIndex
	limits := map[protoreflect.Name]int{
		"frame_digests": maxDiskFrames, "frame_sizes": maxDiskFrames,
		"start_frames": MaxTraceReads, "recent_frames": maxDiskFrames,
	}
	if err := unmarshalStored(diskIndexMagic, b, &msg, limits); err != nil {
		return DiskIndex{}, err
	}
	d := DiskIndex{Size: msg.GetSize(), Start: msg.GetStartFrames(), Recent: msg.GetRecentFrames()}
	digests, sizes := msg.GetFrameDigests(), msg.GetFrameSizes()
	if len(digests) != len(sizes) {
		return DiskIndex{}, fmt.Errorf("%w: %d frame digests for %d sizes", ErrInvalidIndex, len(digests), len(sizes))
	}
	d.Frames = make([]DiskFrame, len(sizes))
	for i, size := range sizes {
		if (size == 0) != (len(digests[i]) == 0) || len(digests[i]) != 0 && len(digests[i]) != sha256.Size {
			return DiskIndex{}, fmt.Errorf("%w: frame %d has a %d byte digest and %d bytes", ErrInvalidIndex, i, len(digests[i]), size)
		}
		d.Frames[i] = DiskFrame{Size: size}
		copy(d.Frames[i].Digest[:], digests[i])
	}
	if err := d.validate(); err != nil {
		return DiskIndex{}, fmt.Errorf("%w: %w", ErrInvalidIndex, err)
	}
	return d, nil
}

func (d DiskIndex) validate() error {
	if d.Size <= 0 || d.Size%hostproto.DiskBlockBytes != 0 || d.Size > hostproto.MaxDiskBytes {
		return fmt.Errorf("disk of %d bytes", d.Size)
	}
	if len(d.Frames) != DiskFrames(d.Size) {
		return fmt.Errorf("%d frames for a %d byte disk", len(d.Frames), d.Size)
	}
	for i, f := range d.Frames {
		if f.Size < 0 || f.Size > maxPackedFrame {
			return fmt.Errorf("frame %d of %d bytes", i, f.Size)
		}
	}
	if len(d.Start) > MaxTraceReads {
		return fmt.Errorf("%d start frames", len(d.Start))
	}
	for _, list := range [][]uint32{d.Start, d.Recent} {
		for _, i := range list {
			if int(i) >= len(d.Frames) {
				return fmt.Errorf("frame %d of %d named for prefetch", i, len(d.Frames))
			}
		}
	}
	return nil
}

// DiskFrameEncoder compresses disk frames for storage. Encode may be called
// from several goroutines at once.
type DiskFrameEncoder struct{ enc *zstd.Encoder }

// NewDiskFrameEncoder returns an encoder running up to concurrency encodes
// at once.
func NewDiskFrameEncoder(concurrency int) (*DiskFrameEncoder, error) {
	enc, err := zstd.NewWriter(nil, zstd.WithEncoderConcurrency(concurrency), zstd.WithEncoderCRC(true),
		zstd.WithWindowSize(FrameSize), zstd.WithLowerEncoderMem(true))
	if err != nil {
		return nil, fmt.Errorf("start frame encoder: %w", err)
	}
	return &DiskFrameEncoder{enc: enc}, nil
}

// Encode returns the frame data is and its stored copy; a frame of zeros
// has none.
func (e *DiskFrameEncoder) Encode(data []byte) (DiskFrame, []byte) {
	if len(data) == 0 || data[0] == 0 && bytes.Equal(data[1:], data[:len(data)-1]) {
		return DiskFrame{}, nil
	}
	packed := e.enc.EncodeAll(data, make([]byte, 0, len(data)/2))
	return DiskFrame{Digest: sha256.Sum256(data), Size: int64(len(packed))}, packed
}

// DecodeDiskFrame returns the bytes of a stored disk frame f of want bytes,
// checked against its digest.
func (r *FrameReader) DecodeDiskFrame(packed []byte, f DiskFrame, want int) ([]byte, error) {
	if int64(len(packed)) != f.Size {
		return nil, fmt.Errorf("%w: stored frame %s has %d bytes, the index says %d", ErrInvalidIndex, f.Name(), len(packed), f.Size)
	}
	out, err := r.dec.DecodeAll(packed, make([]byte, 0, want))
	if err != nil {
		return nil, fmt.Errorf("%w: frame %s: %w", ErrInvalidIndex, f.Name(), err)
	}
	if len(out) != want || sha256.Sum256(out) != f.Digest {
		return nil, fmt.Errorf("%w: frame %s does not hold the %d bytes it names", ErrInvalidIndex, f.Name(), want)
	}
	return out, nil
}
