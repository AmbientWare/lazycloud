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

Done on 2026-10-05 on `perf-lazy-format`.

- `internal/imagefs`: `Convert`, `Marshal`, `Unmarshal`, `HTTPObject`,
  `ReadFrame`, `FrameSpan` with the contract's names, plus
  `Index.StreamSize` (see the contract change below). Typed errors:
  `ErrInvalidLayer`, `ErrUnsupportedVersion`, `ErrInvalidIndex` and
  `*StatusError` for a store's refusal.
- `contracts/imagefs/v1/index.proto`, generated into
  `internal/imagefs/indexproto`. Each binding package now runs
  `buf generate --path` for its own contract directory (`hostproto` for
  `contracts/host`). `check.sh` regenerates every protobuf binding into a
  temporary directory and fails on any difference; I checked it fails on a
  stale `index.pb.go`.
- depguard's host runtime list includes `internal/imagefs`.
- `github.com/klauspost/compress` is now direct. No other new dependency.

### Format details the other packets rely on

- The stored index is `LCIX`, a big-endian uint32 version (1), then the
  protobuf message compressed as one zstd frame. Encoding is
  deterministic.
- Frames store sizes only; offsets are the running sum, so the table is
  contiguous by construction.
- A file of 4 MiB or less always sits in one frame: when it would cross a
  boundary the stream is zero-padded to the next one. Larger files are not
  padded.
- A whiteout is a character device 0:0 at the removed path with
  `Whiteout` set. The opaque marker sets `Opaque` on its directory. Other
  `.wh..wh.` names (aufs metadata) are skipped.
- A whiteout hides only lower layers. It never replaces or removes an entry
  of the same layer: a file at that path stays, a directory stays and turns
  opaque, and a directory that lands on a whited-out path (or is added as a
  parent below one) is opaque.
- Old GNU sparse members are regular files with their holes filled, as
  archive/tar reads them.
- Every data frame and the index carry zstd's content checksum, and reads
  verify it: a changed frame is `ErrInvalidIndex`, not wrong bytes.
- One entry per path. A later member replaces an earlier one in place; a
  non-directory replacing a directory removes what was below it. Missing
  parents are added as root-owned 0755 directories with mtime 0. The root
  is `.` and appears only if the tar has it or an opaque marker needs it.
- A hard link's `LinkTarget` is always the file's first entry, never
  another link, and it repeats that entry's `Offset` and `Size`.
- `Unmarshal` decodes at most 64 MiB (a 1 GiB layer's index is 3.0 MB
  decoded, 408 KB stored) and counts records with protowire before
  decoding them: at most 2,097,152 entries and 65,536 frames (a 256 GiB
  layer). It then validates: digest form, frame count against
  `StreamSize`, frame sizes, file bytes inside the stream, clean unique
  paths, each directory before its children, hard links to an earlier
  non-whiteout entry with the same `Offset` and `Size`, marker types.
  `Convert` runs the same check on its own output.
- `HTTPObject` requires 206 with the exact `Content-Range`, returns exactly
  n bytes, and strips the query (the signature) from transport errors.

## Evidence

Tests, all passing with `go test -race ./internal/imagefs/...`
(`LAZYCLOUD_TEST_OBJECT_STORE_ENDPOINT` set for Garage):

- `TestPythonLayersRoundTrip`: `docker save` of python:3.12-slim. Every
  layer's digest equals the image's diff_id, the decoded index equals the
  converted one, and every tar member matches its entry field by field;
  every file reads back byte for byte through `ReadFrame`, and no file of
  4 MiB or less spans two frames. Layer sizes: 81 MB tar to 20 frames and
  26.4 MB data; 38 MB to 9 frames and 11.6 MB; 4 MB and 5 KB layers too.
- `TestConvertKeepsEveryEntryKind`: a built tar with root, xattrs
  (including binary `security.capability`), opaque dir, whiteout, aufs
  metadata, setuid, sticky, symlink, char and block devices, fifo, a chain
  of hard links, `..` and leading `/` in names, a file that triggers
  padding, an empty file, a replaced file and a directory replaced by a
  file. The expected entries are written out in full.
- `TestConvertRefusesLayersItCannotIndex`: hard link to a missing file,
  replaced hard link target, file below a file, truncated tar.
- `TestUnmarshalRefusesUnknownVersionsAndBrokenIndexes`: version 2 gives
  `ErrUnsupportedVersion`; child before its directory, bytes past the
  stream, missing frame, unclean path, truncation, and a hard link that
  dangles, names a whiteout or names other bytes give `ErrInvalidIndex`.
- `TestWhiteoutsHideOnlyLowerLayers`: whiteout after a file and after a
  directory, before a file, before a directory and before a child.
- `TestConvertFillsSparseFiles`: a hand-built old GNU sparse member reads
  back with its holes as zeros.
- `TestReadFrameRefusesCorruptFrames`: one flipped bit at three places in a
  stored frame each gives `ErrInvalidIndex`; with the checksum off one of
  them returned 4 MiB of wrong bytes.
- `TestUnmarshalCountsRecordsBeforeDecoding`: indexes of 2M+1 empty entries
  (474 bytes stored) and 32M frames (3.6 KB stored) are refused. Without
  the count they allocated 663 MB and 3.5 GB before validation failed.
- `TestHTTPObjectReadsFramesThroughPresignedURLs`: a 10 MiB file in Garage
  (own compose project `perf-lazy-format`, port 24900, settings from
`storagetest.Config()`; depguard's host runtime rule runs in lax mode with
that one package allowed). Frame 0 reads
  through a 2 s presigned URL; once it expires Garage answers 400 (S3
  answers 403); the test swaps in a fresh URL and the remaining frames read
  back byte for byte. A failed request's error holds no signature.

1 GiB conversion, the largest pytorch/torchserve:0.12.0-cpu layer (diff_id
`sha256:4395a253...`), from local disk to a local file, this machine
(24 cores, one used):

| Measure | Value |
| --- | --- |
| Tar | 1,070,505,472 bytes, 26,516 entries, 24,441 files |
| Convert | 4.48 to 4.71 s over two runs, about 230 MB/s |
| Peak Go heap in use / max RSS | 41 MiB / 57 MiB |
| Data object | 285,990,412 bytes in 257 frames (gzip layer: 324,604,763) |
| Padding and replaced bytes in the stream | 25.6 MB, 2.4% |
| Index | 408,168 bytes; marshal 14 ms, unmarshal 15 ms |
| ReadFrame from local disk | 4.3 ms per frame |

The image's 924 MB layer took 4.70 s with the same 41 MiB peak heap, so
memory does not grow with the layer. zstd's fastest level was
22% faster and 8.6% larger; the default level stays, since frames are
read many more times than written.

## Contract change

Accepted by the integrator on 2026-10-05.

- `Index.StreamSize int64`, the uncompressed stream length. Without it a
  reader cannot know the last frame's length, so an index could name bytes
  past the end of the stream and the snapshotter would slice out of range.
  `Unmarshal` and `ReadFrame` check against it.

## Intentional differences

- A layer that replaces or removes an entry a hard link names fails with
  `ErrInvalidLayer`. Extraction would split the link into an independent
  file; Docker and BuildKit layers never do this.
- Conversion runs on one core. A bounded pipeline of frame encoders would
  scale with cores if build time needs it.

## Gaps and unverified boundaries

- `HTTPObject` against S3 is unverified here; the acceptance packet runs
  it.
- Frames carry zstd's checksum but no content digest; the integrator chose
  no separate frame digest for now.
- The real-layer test needs Docker and pulls python:3.12-slim if absent.
