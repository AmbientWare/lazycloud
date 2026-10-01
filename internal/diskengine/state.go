package diskengine

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"golang.org/x/sys/unix"
)

// maxSocketPath is sun_path's capacity less the terminating NUL.
const maxSocketPath = 107

type diskPaths struct {
	root string
	id   string
}

func (p diskPaths) dir() string       { return filepath.Join(p.root, p.id) }
func (p diskPaths) statePath() string { return filepath.Join(p.dir(), "state.json") }
func (p diskPaths) layerDir() string  { return filepath.Join(p.dir(), "layers") }
func (p diskPaths) runDir() string    { return filepath.Join(p.dir(), "run") }
func (p diskPaths) qmpSocket() string { return filepath.Join(p.runDir(), "qmp.sock") }
func (p diskPaths) nbdSocket() string { return filepath.Join(p.runDir(), "nbd.sock") }
func (p diskPaths) pidFile() string   { return filepath.Join(p.runDir(), "qsd.pid") }
func (p diskPaths) layerPath(l layer) string {
	return filepath.Join(p.layerDir(), l.file())
}

// Locks live outside the disk directory so evicting a disk cannot hand a
// waiting caller a lock on an unlinked file while a new directory appears.
// Disk ids never contain a dot, so the NBD lock cannot collide with one.
func (p diskPaths) lockPath() string    { return filepath.Join(p.root, ".locks", p.id) }
func (p diskPaths) nbdLockPath() string { return filepath.Join(p.root, ".locks", "nbd.lock") }

func (p diskPaths) checkSocketPaths() error {
	for _, path := range []string{p.qmpSocket(), p.nbdSocket()} {
		if len(path) > maxSocketPath {
			return fmt.Errorf("%w: socket path %s is %d bytes, over the %d a unix socket allows; use a shorter root",
				ErrInvalid, path, len(path), maxSocketPath)
		}
	}
	return nil
}

// layerFormat is how a layer file stores its contents.
type layerFormat string

const (
	formatQcow2 layerFormat = "qcow2"
	formatRaw   layerFormat = "raw"
)

// layer is one file in the local chain. The last layer is the writable head.
// Every layer below it is sealed and never written again, except by a
// compaction committing published layers into the base.
type layer struct {
	Seq int `json:"seq"`
	// Generation is the published generation whose content the chain up to
	// and including this layer equals; 0 while the layer is unpublished.
	Generation int64 `json:"generation"`
	// Raw marks a base restored from a flattened generation, which holds the
	// disk's contents as a sparse raw file rather than as qcow2.
	Raw bool `json:"raw,omitempty"`
}

func (l layer) format() layerFormat {
	if l.Raw {
		return formatRaw
	}
	return formatQcow2
}

func (l layer) file() string { return fmt.Sprintf("%06d.%s", l.Seq, l.format()) }
func (l layer) node() string { return fmt.Sprintf("layer%d", l.Seq) }

type attachment struct {
	Mountpoint string `json:"mountpoint"`
	DaemonPID  int    `json:"daemon_pid"`
	Device     string `json:"device,omitempty"`
	Mounted    bool   `json:"mounted"`
}

// publishResult is an upload as the state file records it.
type publishResult struct {
	ManifestKey      string `json:"manifest_key"`
	ManifestSHA256   string `json:"manifest_sha256"`
	StoredBytesAdded int64  `json:"stored_bytes_added"`
	Generation       int64  `json:"generation"`
	ParentGeneration int64  `json:"parent_generation"`
}

// pendingPublish is an upload whose generation the control plane has not yet
// confirmed. A retried publish of the same layer returns it unchanged, so
// the added bytes are not lost to chunks the first attempt already stored.
type pendingPublish struct {
	Seq    int           `json:"seq"`
	Flat   bool          `json:"flatten"`
	Result publishResult `json:"result"`
}

func (p *pendingPublish) published() *Published {
	return &Published{
		Generation:       p.Result.Generation,
		ParentGeneration: p.Result.ParentGeneration,
		ManifestKey:      p.Result.ManifestKey,
		ManifestSHA256:   p.Result.ManifestSHA256,
		AddedBytes:       p.Result.StoredBytesAdded,
		Flat:             p.Flat,
	}
}

// publishedRecord is a generation this host knows the control plane recorded,
// kept so collect can tell which manifests and chunks are still reachable.
type publishedRecord struct {
	Generation       int64  `json:"generation"`
	ParentGeneration int64  `json:"parent_generation"`
	ManifestKey      string `json:"manifest_key"`
	ManifestSHA256   string `json:"manifest_sha256"`
}

type diskState struct {
	DiskID    string  `json:"disk_id"`
	SizeBytes int64   `json:"size_bytes"`
	Layers    []layer `json:"layers"`
	NextSeq   int     `json:"next_seq"`
	// HeadFresh is true while the engine created the head empty under the
	// running daemon, so the daemon's write statistics cover all of it.
	HeadFresh bool `json:"head_fresh"`
	// Unformatted is true from creating a new disk's base until mkfs has
	// written its filesystem, so an attach that stopped between the two
	// formats it next time instead of mounting an empty device.
	Unformatted bool `json:"unformatted,omitempty"`
	// GrowFilesystem is true while the disk has grown and its ext4 has not
	// yet been resized to fill it.
	GrowFilesystem          bool              `json:"grow_filesystem,omitempty"`
	PublishedGeneration     int64             `json:"published_generation"`
	PublishedManifestSHA256 string            `json:"published_manifest_sha256"`
	Published               []publishedRecord `json:"published"`
	Pending                 *pendingPublish   `json:"pending_publish,omitempty"`
	Attachment              *attachment       `json:"attachment,omitempty"`
	LastUsedAt              time.Time         `json:"last_used_at"`
}

func (s *diskState) record(generation int64) (publishedRecord, bool) {
	for _, record := range s.Published {
		if record.Generation == generation {
			return record, true
		}
	}
	return publishedRecord{}, false
}

func (s *diskState) head() layer { return s.Layers[len(s.Layers)-1] }

func (s *diskState) newLayer() layer {
	s.NextSeq++
	return layer{Seq: s.NextSeq}
}

func (s *diskState) oldestUnpublished() int {
	for i := range len(s.Layers) - 1 {
		if s.Layers[i].Generation == 0 {
			return i
		}
	}
	return -1
}

func (s *diskState) unpublishedSealed() int {
	count := 0
	for i := range len(s.Layers) - 1 {
		if s.Layers[i].Generation == 0 {
			count++
		}
	}
	return count
}

// chainDepth counts the committed generations from the newest parentless one
// up to the newest committed generation.
func (s *diskState) chainDepth() (int, error) {
	depth := 0
	for generation := s.PublishedGeneration; generation != 0; depth++ {
		record, known := s.record(generation)
		if !known {
			return 0, fmt.Errorf("disk %s has no record of generation %d", s.DiskID, generation)
		}
		generation = record.ParentGeneration
	}
	return depth, nil
}

// commitPending marks the pending upload's layer as its generation.
func (s *diskState) commitPending() error {
	pending := s.Pending
	for i := range s.Layers {
		if s.Layers[i].Seq != pending.Seq {
			continue
		}
		s.Layers[i].Generation = pending.Result.Generation
		s.Published = append(s.Published, publishedRecord{
			Generation:       pending.Result.Generation,
			ParentGeneration: pending.Result.ParentGeneration,
			ManifestKey:      pending.Result.ManifestKey,
			ManifestSHA256:   pending.Result.ManifestSHA256,
		})
		s.PublishedGeneration = pending.Result.Generation
		s.PublishedManifestSHA256 = pending.Result.ManifestSHA256
		s.Pending = nil
		return nil
	}
	return fmt.Errorf("disk %s no longer holds the layer uploaded as generation %d", s.DiskID, pending.Result.Generation)
}

// loadState returns nil without error for a disk with no local state.
func loadState(p diskPaths) (*diskState, error) {
	data, err := os.ReadFile(p.statePath())
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil // Absence is not a failure.
	}
	if err != nil {
		return nil, fmt.Errorf("read disk state: %w", err)
	}
	var state diskState
	if err := json.Unmarshal(data, &state); err != nil {
		return nil, fmt.Errorf("parse %s: %w", p.statePath(), err)
	}
	if state.DiskID != p.id || len(state.Layers) == 0 {
		return nil, fmt.Errorf("%s does not describe disk %s", p.statePath(), p.id)
	}
	return &state, nil
}

func requireState(p diskPaths) (*diskState, error) {
	state, err := loadState(p)
	if err != nil {
		return nil, err
	}
	if state == nil {
		return nil, fmt.Errorf("%w: disk %s has no local state under %s", ErrInvalid, p.id, p.root)
	}
	return state, nil
}

func saveState(p diskPaths, state *diskState) error {
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return fmt.Errorf("encode disk state: %w", err)
	}
	return writeFileAtomic(p.statePath(), data)
}

func writeFileAtomic(path string, data []byte) error {
	tmp := path + ".tmp"
	file, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600) //nolint:gosec // A state path under the root.
	if err != nil {
		return fmt.Errorf("create %s: %w", tmp, err)
	}
	if _, err := file.Write(data); err != nil {
		return errors.Join(fmt.Errorf("write %s: %w", tmp, err), file.Close())
	}
	if err := file.Sync(); err != nil {
		return errors.Join(fmt.Errorf("sync %s: %w", tmp, err), file.Close())
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close %s: %w", tmp, err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fmt.Errorf("replace %s: %w", path, err)
	}
	return syncDir(filepath.Dir(path))
}

func syncDir(path string) error {
	dir, err := os.Open(path) //nolint:gosec // A directory under the root.
	if err != nil {
		return fmt.Errorf("open %s: %w", path, err)
	}
	if err := dir.Sync(); err != nil {
		return errors.Join(fmt.Errorf("sync %s: %w", path, err), dir.Close())
	}
	if err := dir.Close(); err != nil {
		return fmt.Errorf("close %s: %w", path, err)
	}
	return nil
}

// lockPoll is how often a waiting caller retries a held lock.
const lockPoll = 50 * time.Millisecond

type fileLock struct{ file *os.File }

// lockFile takes an exclusive flock on path, waiting until it is free or ctx
// ends. flock binds to the open file, so two goroutines of one process
// exclude each other as two processes do.
func lockFile(ctx context.Context, path string) (*fileLock, error) {
	for {
		lock, err := tryLockFile(path)
		if err != nil || lock != nil {
			return lock, err
		}
		select {
		case <-ctx.Done():
			return nil, fmt.Errorf("wait for lock %s: %w", path, ctx.Err())
		case <-time.After(lockPoll):
		}
	}
}

// tryLockFile returns nil without waiting when another holder has the lock.
func tryLockFile(path string) (*fileLock, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, fmt.Errorf("create lock directory: %w", err)
	}
	file, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600) //nolint:gosec // A lock path under the root.
	if err != nil {
		return nil, fmt.Errorf("open lock %s: %w", path, err)
	}
	if err := unix.Flock(int(file.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil { //nolint:gosec // Descriptors fit an int.
		if closeErr := file.Close(); closeErr != nil {
			return nil, fmt.Errorf("close lock %s: %w", path, closeErr)
		}
		if errors.Is(err, unix.EWOULDBLOCK) {
			return nil, nil // Another holder has the lock.
		}
		return nil, fmt.Errorf("lock %s: %w", path, err)
	}
	return &fileLock{file: file}, nil
}

// release drops the lock. Closing the descriptor releases the flock, and a
// lock file holds no data a failed close could lose.
func (l *fileLock) release() { _ = l.file.Close() }

func lockDisk(ctx context.Context, p diskPaths) (*fileLock, error) {
	return lockFile(ctx, p.lockPath())
}
