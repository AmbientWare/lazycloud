# Format

## Scope

The library the build path writes with and the snapshotter reads with. Owns:

- new package `internal/imagefs` (no database access; add it to depguard's
  host runtime list);
- `contracts/imagefs/v1/index.proto` and its generated Go code (`buf`, as the
  host protocol does), so the stored index has one language-neutral schema;
- `go.mod`: `github.com/klauspost/compress` becomes a direct dependency for
  zstd. No other new dependency without a reason in the report.

Stay off the agent, images, compute, the host protocol and the snapshotter.

## The contract other packets code against

Keep these names and shapes; propose any change in the report.

```go
package imagefs

// Digest is a layer's uncompressed digest (diff_id), "sha256:<hex>".
type Digest string

// FrameSize is the uncompressed size of every data frame but the last.
const FrameSize = 4 << 20

// Index is one layer's file tree and the frame table of its data object.
type Index struct {
	Layer    Digest
	DataSize int64   // bytes of the stored, compressed data object
	Frames   []Frame // frame i holds uncompressed bytes [i*FrameSize, (i+1)*FrameSize)
	Entries  []Entry // in tar order; directories before their children
}

type Frame struct {
	Offset int64 // in the data object
	Size   int64 // compressed bytes
}

type Entry struct {
	Path               string // clean, relative, no leading "./"
	Type               EntryType
	Mode               fs.FileMode
	UID, GID           uint32
	ModTime            time.Time
	LinkTarget         string // symlinks and hard links
	DevMajor, DevMinor uint32
	Xattrs             map[string][]byte
	Whiteout, Opaque   bool  // OCI whiteout and opaque directory markers
	Offset, Size       int64 // a regular file's bytes in the uncompressed data stream
}

// Convert reads an uncompressed OCI layer tar, writes the compressed data
// object to data as it goes, and returns the index. Memory stays bounded
// by a few frames whatever the layer size.
func Convert(ctx context.Context, layer io.Reader, data io.Writer) (Index, error)

func (ix Index) Marshal() ([]byte, error)
func Unmarshal(b []byte) (Index, error) // refuses an unknown format version

// RangeReader reads bytes [off, off+n) of a stored object.
type RangeReader interface {
	ReadRange(ctx context.Context, off, n int64) (io.ReadCloser, error)
}

// HTTPObject reads an object through presigned URLs; url returns the
// current one, so a refreshed grant takes effect on the next read.
func HTTPObject(client *http.Client, url func() string) RangeReader

// ReadFrame returns frame i's uncompressed bytes.
func (ix Index) ReadFrame(ctx context.Context, data RangeReader, i int) ([]byte, error)

// FrameSpan is the frames holding bytes [off, off+n) of e's contents.
func (ix Index) FrameSpan(e Entry, off, n int64) (first, last int)
```

## Plan

- Hard links: the second and later links point at the first entry's bytes;
  no duplicate data.
- Empty files and files of one frame or less read with one range request.
- A frame decompresses independently (one zstd frame each).

## Evidence to record

- Round trip of real layers through `Convert`, `Marshal`, `Unmarshal` and
  `ReadFrame`: a Python base layer and a layer with hard links, symlinks,
  devices, whiteouts, an opaque directory and xattrs, compared entry by entry
  and byte for byte with the tar.
- Memory and time to convert a 1 GiB layer.
- `HTTPObject` against the local Garage with a presigned URL, including a URL
  swapped mid-read.

## Progress

## Intentional differences

## Gaps and unverified boundaries
