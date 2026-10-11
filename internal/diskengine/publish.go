package diskengine

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// frameWorkers bounds the frames one publish reads, compresses and uploads
// at once, each holding one frame in memory.
const frameWorkers = 16

// Publish uploads, as the next generation, everything the sealed layers
// hold: each frame they changed, read whole over the base generation, is
// stored unless the bucket holds it, and the generation's index names every
// frame of the disk. It never seals. A disk with nothing sealed publishes
// only to record a new start trace, or with final the frames the host's
// cache holds for it, and returns nil when neither changed. The upload stays
// pending until CommitPublished; until then a retry returns it.
func (e *Engine) Publish(ctx context.Context, diskID string, store Store, final bool) (*Published, error) {
	var out *Published
	err := e.withDisk(ctx, diskID, requireState, func(p diskPaths, state *diskState) error {
		if state.Pending != nil {
			out = state.Pending.published()
			return nil
		}
		objects, err := openStore(store)
		if err != nil {
			return err
		}
		ctx, span := telemetry.Start(ctx, "diskengine.publish", trace.WithAttributes(attribute.Int("lazycloud.layers", len(state.sealed()))))
		pending, err := e.publish(ctx, p, state, objects, final)
		if pending != nil {
			span.SetAttributes(attribute.Int64("lazycloud.bytes", pending.AddedBytes))
		}
		telemetry.Fail(span, err)
		if err != nil || pending == nil {
			return err
		}
		e.log.InfoContext(ctx, "disk generation uploaded", "disk_id", p.id, "generation", pending.Generation, "added_bytes", pending.AddedBytes)
		out = pending.published()
		return nil
	})
	return out, err
}

func (e *Engine) publish(ctx context.Context, p diskPaths, state *diskState, objects imagefs.Bucket, final bool) (*pendingPublish, error) {
	base, err := loadBase(p, state)
	if err != nil {
		return nil, err
	}
	next := imagefs.DiskIndex{Size: state.SizeBytes, Frames: make([]imagefs.DiskFrame, imagefs.DiskFrames(state.SizeBytes)),
		Start: base.Start, Recent: base.Recent}
	copy(next.Frames, base.Frames)
	// A disk the snapshotter does not serve, as after a detach or its
	// restart, has no reads to report and keeps those recorded.
	reads, err := e.bases.DiskReads(ctx, &imagefsproto.DiskReadsRequest{DiskId: p.id})
	switch {
	case status.Code(err) == codes.NotFound:
	case err != nil:
		return nil, fmt.Errorf("read the disk's reads: %w", err)
	default:
		if len(reads.GetStartFrames()) > 0 {
			next.Start = reads.GetStartFrames()
		}
		if final {
			next.Recent = reads.GetRecentFrames()
		}
	}
	sealed := state.sealed()
	extents, dirty, err := dirtyFrames(ctx, p, sealed)
	if err != nil {
		return nil, err
	}
	grew := state.Base != nil && base.Size < next.Size
	if len(dirty) == 0 && !grew && slices.Equal(next.Start, base.Start) && slices.Equal(next.Recent, base.Recent) {
		return nil, nil // Nothing new to publish.
	}
	// A partial last frame of the base reads on into the grown disk.
	if last := len(base.Frames) - 1; grew && !base.Frames[last].Zero() && !slices.Contains(dirty, uint32(last)) { //nolint:gosec // Frame counts fit a uint32.
		dirty = append(dirty, uint32(last)) //nolint:gosec // Frame counts fit a uint32.
	}
	basePath := ""
	if state.Base != nil {
		if basePath, err = e.serveBase(ctx, p.id, *state.Base, p.baseIndex(), ""); err != nil {
			return nil, err
		}
	}
	generation := generationOf(state.Base) + 1
	added, err := storeFrames(ctx, p, objects, sealed, extents, base, basePath, dirty, &next)
	if err != nil {
		return nil, fmt.Errorf("publish generation %d of disk %s: %w", generation, p.id, err)
	}
	raw, err := next.Marshal()
	if err != nil {
		return nil, err //nolint:wrapcheck // The format names itself.
	}
	sum := sha256.Sum256(raw)
	digest := hex.EncodeToString(sum[:])
	if err := objects.Put(ctx, imagefs.DiskIndexKey(p.id, generation, digest), raw); err != nil {
		return nil, err
	}
	if err := writeFileAtomic(p.pendingIndex(), raw); err != nil {
		return nil, err
	}
	held := map[[sha256.Size]byte]bool{}
	for _, f := range next.Frames {
		held[f.Digest] = true
	}
	var collect []collectKey
	freed := map[[sha256.Size]byte]bool{}
	for _, f := range base.Frames {
		if !f.Zero() && !held[f.Digest] && !freed[f.Digest] {
			freed[f.Digest] = true
			collect = append(collect, collectKey{Key: imagefs.DiskPrefix(p.id) + f.Name(), Bytes: f.Size})
		}
	}
	if b := state.Base; b != nil {
		collect = append(collect, collectKey{Key: imagefs.DiskIndexKey(p.id, b.Generation, b.IndexSHA256)})
	}
	// A frame an earlier collection would delete may be one this
	// generation names again.
	if state.Collect != nil {
		state.Collect.Keys = slices.DeleteFunc(state.Collect.Keys, func(k collectKey) bool {
			name, isFrame := strings.CutPrefix(k.Key, imagefs.DiskPrefix(p.id)+"frames/")
			sum, err := hex.DecodeString(name)
			return isFrame && err == nil && len(sum) == sha256.Size && held[[sha256.Size]byte(sum)]
		})
	}
	state.Pending = &pendingPublish{Generation: generation, IndexSHA256: digest, AddedBytes: added, Collect: collect}
	if len(sealed) > 0 {
		state.Pending.Through = sealed[len(sealed)-1].Seq
	}
	if err := saveState(p, state); err != nil {
		return nil, err
	}
	return state.Pending, nil
}

// loadBase returns the index of the generation the stack is on, which the
// snapshotter keeps beside the stack when it first serves it; a disk never
// published has an empty one.
func loadBase(p diskPaths, state *diskState) (imagefs.DiskIndex, error) {
	if state.Base == nil {
		return imagefs.DiskIndex{}, nil
	}
	raw, err := os.ReadFile(p.baseIndex())
	if err != nil {
		return imagefs.DiskIndex{}, fmt.Errorf("read the base index: %w", err)
	}
	return imagefs.UnmarshalDisk(raw) //nolint:wrapcheck // The format names itself.
}

// serveBase has the snapshotter serve generation g, whose index the engine
// keeps at index, and returns its file, first moving into its cache the
// frames in the frames directory, if any.
func (e *Engine) serveBase(ctx context.Context, diskID string, g Generation, index, frames string) (string, error) {
	served, err := e.bases.ServeDisk(ctx, &imagefsproto.ServeDiskRequest{
		DiskId: diskID, Generation: g.Generation, IndexSha256: g.IndexSHA256, IndexPath: index, FramesDir: frames,
	})
	if err != nil {
		return "", fmt.Errorf("serve the base generation: %w", err)
	}
	return served.GetPath(), nil
}

// mappedExtent is one range of `qemu-img map --output=json`.
type mappedExtent struct {
	Start   int64  `json:"start"`
	Length  int64  `json:"length"`
	Depth   int    `json:"depth"`
	Zero    bool   `json:"zero"`
	Data    bool   `json:"data"`
	Present bool   `json:"present"`
	Offset  *int64 `json:"offset"`
}

// chainSpec opens layers, top first, as one image without the base, as
// qemu-img's json: filename takes it.
func chainSpec(p diskPaths, layers []layer) (string, error) {
	var spec any // JSON null below the bottom layer
	for _, l := range layers {
		spec = map[string]any{"driver": "qcow2", "file": map[string]any{"driver": "file", "filename": p.layerPath(l)}, "backing": spec}
	}
	raw, err := json.Marshal(spec)
	if err != nil {
		return "", fmt.Errorf("encode the layer chain: %w", err)
	}
	return "json:" + string(raw), nil
}

// mapLayers lists the ranges layers, base first, hold, sharing the files
// with a running daemon.
func mapLayers(ctx context.Context, p diskPaths, layers []layer) ([]mappedExtent, error) {
	spec, err := chainSpec(p, layers)
	if err != nil {
		return nil, err
	}
	out, err := runTool(ctx, toolImage, "map", "-U", "--output=json", spec)
	if err != nil {
		return nil, err
	}
	var extents []mappedExtent
	if err := json.Unmarshal([]byte(out), &extents); err != nil {
		return nil, fmt.Errorf("parse qemu-img map of disk %s: %w", p.id, err)
	}
	return slices.DeleteFunc(extents, func(x mappedExtent) bool { return !x.Present }), nil
}

// dirtyFrames lists the ranges layers hold and the frames they write any
// of, ascending.
func dirtyFrames(ctx context.Context, p diskPaths, layers []layer) ([]mappedExtent, []uint32, error) {
	if len(layers) == 0 {
		return nil, nil, nil
	}
	extents, err := mapLayers(ctx, p, layers)
	if err != nil {
		return nil, nil, err
	}
	var dirty []uint32
	for _, x := range extents {
		for i := x.Start / imagefs.FrameSize; i <= (x.Start+x.Length-1)/imagefs.FrameSize; i++ {
			if n := len(dirty); n == 0 || dirty[n-1] < uint32(i) { //nolint:gosec // Frame counts fit a uint32.
				dirty = append(dirty, uint32(i)) //nolint:gosec // Frame counts fit a uint32.
			}
		}
	}
	return extents, dirty, nil
}

// storeFrames reads each dirty frame as the sealed stack shows it, the base
// generation's bytes at basePath under the sealed layers' writes, stores
// those the bucket lacks, filling their entries in next, and keeps each in
// the disk's frames directory for the snapshotter's cache. It returns the
// bytes stored.
func storeFrames(ctx context.Context, p diskPaths, objects imagefs.Bucket, sealed []layer, extents []mappedExtent,
	base imagefs.DiskIndex, basePath string, dirty []uint32, next *imagefs.DiskIndex,
) (int64, error) {
	if err := os.RemoveAll(p.framesDir()); err != nil {
		return 0, fmt.Errorf("clear the frames directory: %w", err)
	}
	if err := os.Mkdir(p.framesDir(), 0o700); err != nil {
		return 0, fmt.Errorf("create the frames directory: %w", err)
	}
	var err error
	files := make([]*os.File, len(sealed))
	defer func() {
		for _, f := range files {
			if f != nil {
				_ = f.Close() // Read only.
			}
		}
	}()
	for i, l := range sealed {
		if files[i], err = os.Open(p.layerPath(l)); err != nil {
			return 0, fmt.Errorf("open layer: %w", err)
		}
	}
	var baseFile *os.File
	if basePath != "" {
		// O_NOATIME keeps these reads out of the disk's start trace and
		// recent frames.
		if baseFile, err = os.OpenFile(basePath, os.O_RDONLY|syscall.O_NOATIME, 0); err != nil { //nolint:gosec // The snapshotter's file.
			return 0, fmt.Errorf("open the base generation: %w", err)
		}
		defer func() { _ = baseFile.Close() }() // Read only.
	}
	stored := map[[sha256.Size]byte]bool{}
	for _, f := range base.Frames {
		stored[f.Digest] = !f.Zero()
	}
	encoder, err := imagefs.NewDiskFrameEncoder(frameWorkers)
	if err != nil {
		return 0, err //nolint:wrapcheck // The encoder names itself.
	}
	var added atomic.Int64
	// Frames of equal bytes are kept and upload once.
	type upload struct {
		once sync.Once
		err  error
	}
	var mu sync.Mutex
	uploads := map[[sha256.Size]byte]*upload{}
	group, groupCtx := errgroup.WithContext(ctx)
	group.SetLimit(frameWorkers)
	for _, frame := range dirty {
		group.Go(func() error {
			i := int(frame)
			data := make([]byte, next.FrameLen(i))
			if baseFile != nil && i < len(base.Frames) && !base.Frames[i].Zero() {
				n := min(len(data), base.FrameLen(i))
				if _, err := baseFile.ReadAt(data[:n], int64(i)*imagefs.FrameSize); err != nil {
					return fmt.Errorf("read frame %d of the base generation: %w", i, err)
				}
			}
			if err := overlay(data, int64(i)*imagefs.FrameSize, extents, files); err != nil {
				return err
			}
			f, packed := encoder.Encode(data)
			next.Frames[i] = f
			if f.Zero() {
				return nil
			}
			mu.Lock()
			u := uploads[f.Digest]
			if u == nil {
				u = &upload{}
				uploads[f.Digest] = u
			}
			mu.Unlock()
			u.once.Do(func() {
				if u.err = os.WriteFile(filepath.Join(p.dir(), f.Name()), data, 0o600); u.err != nil || stored[f.Digest] {
					return
				}
				if u.err = objects.Put(groupCtx, imagefs.DiskPrefix(p.id)+f.Name(), packed); u.err == nil {
					added.Add(f.Size)
				}
			})
			return u.err
		})
	}
	if err := group.Wait(); err != nil {
		return 0, err //nolint:wrapcheck // Each frame names itself.
	}
	return added.Load(), nil
}

// overlay copies into data, the frame at off, what extents hold of it from
// the sealed layers' files, top first by depth.
func overlay(data []byte, off int64, extents []mappedExtent, files []*os.File) error {
	end := off + int64(len(data))
	for _, x := range extents {
		lo, hi := max(x.Start, off), min(x.Start+x.Length, end)
		if lo >= hi {
			continue
		}
		dst := data[lo-off : hi-off]
		if x.Zero || !x.Data {
			clear(dst)
			continue
		}
		if x.Offset == nil || x.Depth < 0 || x.Depth >= len(files) {
			return fmt.Errorf("qemu-img map names depth %d without a file offset at %d", x.Depth, x.Start)
		}
		file := files[len(files)-1-x.Depth]
		if _, err := file.ReadAt(dst, *x.Offset+lo-x.Start); err != nil {
			return fmt.Errorf("read %s: %w", file.Name(), err)
		}
	}
	return nil
}

// CommitPublished makes generation, the pending upload, the disk's base
// once the control plane has recorded it, and keeps the orphans it named
// for Collect. An attached disk's running stack
// moves onto the generation's file, with the frames it replaced cached
// first, and the snapshotter stops serving older generations. The layers
// it holds are deleted. Committing the committed generation again
// succeeds.
func (e *Engine) CommitPublished(ctx context.Context, diskID string, generation int64, orphans []Generation) error {
	return e.withDisk(ctx, diskID, requireState, func(p diskPaths, state *diskState) error {
		if state.Pending == nil || state.Pending.Generation != generation {
			if state.Pending == nil && generationOf(state.Base) == generation {
				return nil
			}
			return fmt.Errorf("%w: disk %s has no uploaded generation %d awaiting commit", ErrInvalid, p.id, generation)
		}
		return e.commit(ctx, p, state, orphans)
	})
}

func (e *Engine) commit(ctx context.Context, p diskPaths, state *diskState, orphans []Generation) error {
	next := state.Pending.generation()
	if a := state.Attachment; a != nil && daemonAlive(p, a.DaemonPID) {
		path, err := e.serveBase(ctx, p.id, next, p.pendingIndex(), p.framesDir())
		if err != nil {
			return err
		}
		if err := telemetry.Step(ctx, "diskengine.rebase", func(ctx context.Context) error { return rebase(ctx, p, state, path) }); err != nil {
			return err
		}
		if a.BasePath, a.BaseDevice, err = served(path); err != nil {
			return err
		}
	}
	held, err := state.commitPending(p)
	if err != nil {
		return err
	}
	state.Collect.Orphans = append(state.Collect.Orphans, orphans...)
	if err := saveState(p, state); err != nil {
		return err
	}
	for _, l := range held {
		if err := removeIfExists(p.layerPath(l)); err != nil {
			return err
		}
	}
	if err := os.RemoveAll(p.framesDir()); err != nil {
		return fmt.Errorf("remove the frames directory: %w", err)
	}
	if _, err := e.bases.ReleaseDisk(ctx, &imagefsproto.ReleaseDiskRequest{DiskId: p.id, Keep: next.Generation}); err != nil {
		return fmt.Errorf("release older generations: %w", err)
	}
	return nil
}

// Remover deletes a batch of the disk's objects, naming the frame bytes
// they hold, and must refuse once the caller no longer holds the disk: a
// holder that lost its lease cannot know what a newer holder stored.
type Remover func(ctx context.Context, generation int64, keys []string, bytes int64) error

// Collect deletes, through remove, the objects the committed generations
// no longer read: the indexes they replaced, frames no later generation
// names, and orphans' indexes and the frames only they name, which it reads
// from store. A batch removed stays removed when a later one fails.
func (e *Engine) Collect(ctx context.Context, diskID string, store Store, remove Remover) error {
	return e.withDisk(ctx, diskID, requireState, func(p diskPaths, state *diskState) error {
		if state.Collect == nil {
			return nil
		}
		if len(state.Collect.Orphans) > 0 && state.Pending == nil {
			if err := collectOrphans(ctx, p, state, store); err != nil {
				return err
			}
		}
		for len(state.Collect.Keys) > 0 {
			batch := state.Collect.Keys[:min(len(state.Collect.Keys), deleteBatchSize)]
			keys := make([]string, len(batch))
			var bytes int64
			for i, k := range batch {
				keys[i], bytes = k.Key, bytes+k.Bytes
			}
			if err := remove(ctx, state.Collect.Generation, keys, bytes); err != nil {
				return fmt.Errorf("collect disk %s: %w", p.id, err)
			}
			state.Collect.Keys = state.Collect.Keys[len(batch):]
			if err := saveState(p, state); err != nil {
				return err
			}
		}
		if len(state.Collect.Orphans) == 0 {
			state.Collect = nil
		}
		return saveState(p, state)
	})
}

// collectOrphans adds to the collection each orphan's index and the frames
// it names that the base does not. Their bytes were never counted as the
// disk's. An index gone or unreadable names no frames.
func collectOrphans(ctx context.Context, p diskPaths, state *diskState, store Store) error {
	objects, err := openStore(store)
	if err != nil {
		return err
	}
	base, err := loadBase(p, state)
	if err != nil {
		return err
	}
	named := map[string]bool{}
	for _, f := range base.Frames {
		named[f.Name()] = true
	}
	for _, o := range state.Collect.Orphans {
		key := imagefs.DiskIndexKey(p.id, o.Generation, o.IndexSHA256)
		raw, err := objects.Get(ctx, key, imagefs.MaxIndexSize)
		if err != nil && !errors.Is(err, fs.ErrNotExist) {
			return err
		}
		if ix, err := imagefs.UnmarshalDisk(raw); err == nil {
			for _, f := range ix.Frames {
				if !f.Zero() && !named[f.Name()] {
					named[f.Name()] = true
					state.Collect.Keys = append(state.Collect.Keys, collectKey{Key: imagefs.DiskPrefix(p.id) + f.Name()})
				}
			}
		}
		state.Collect.Keys = append(state.Collect.Keys, collectKey{Key: key})
	}
	state.Collect.Orphans = nil
	return saveState(p, state)
}
