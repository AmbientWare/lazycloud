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

// baseIndex holds the stored index of the generation the stack is on, and
// pendingIndex that of the generation awaiting commit.
func (p diskPaths) baseIndex() string    { return filepath.Join(p.dir(), "base.index") }
func (p diskPaths) pendingIndex() string { return filepath.Join(p.dir(), "pending.index") }
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

// layer is one qcow2 file of the local stack. The last layer is the
// writable head; every layer below it is sealed and never written again.
// Layers hold only writes since the base generation; none names its
// backing, which the engine always gives.
type layer struct {
	Seq int `json:"seq"`
}

func (l layer) file() string { return fmt.Sprintf("%06d.qcow2", l.Seq) }
func (l layer) node() string { return fmt.Sprintf("layer%d", l.Seq) }
func (l layer) fileNode() string {
	return fmt.Sprintf("file%d", l.Seq)
}

func baseNode(generation int64) string { return fmt.Sprintf("base%d", generation) }

type attachment struct {
	Mountpoint string `json:"mountpoint"`
	DaemonPID  int    `json:"daemon_pid"`
	Device     string `json:"device,omitempty"`
	Mounted    bool   `json:"mounted"`
	// BasePath is the served file the daemon's base node reads, and
	// BaseDevice the device of the snapshotter's mount it is on, which a
	// restarted snapshotter changes.
	BasePath   string `json:"base_path,omitempty"`
	BaseDevice uint64 `json:"base_device,omitempty"`
}

// collectKey is an object a committed generation no longer reads.
type collectKey struct {
	Key   string `json:"key"`
	Bytes int64  `json:"bytes"`
}

// collection is what CollectDisk deletes once Generation is recorded.
type collection struct {
	Generation int64        `json:"generation"`
	Keys       []collectKey `json:"keys"`
}

// pendingPublish is an upload whose generation the control plane has not
// yet confirmed. A retried publish returns it unchanged.
type pendingPublish struct {
	Generation     int64  `json:"generation"`
	ManifestKey    string `json:"manifest_key"`
	ManifestSHA256 string `json:"manifest_sha256"`
	AddedBytes     int64  `json:"added_bytes"`
	// Through is the newest layer the generation holds.
	Through int `json:"through"`
	// Dirty are the frames the generation replaced, cached before the live
	// stack moves onto it.
	Dirty []uint32 `json:"dirty"`
	// Collect is what the committed generation no longer reads.
	Collect []collectKey `json:"collect"`
}

func (p *pendingPublish) published() *Published {
	return &Published{Generation: p.Generation, ManifestKey: p.ManifestKey, ManifestSHA256: p.ManifestSHA256, AddedBytes: p.AddedBytes}
}

type diskState struct {
	DiskID    string `json:"disk_id"`
	SizeBytes int64  `json:"size_bytes"`
	// Base is the committed generation the stack is on; nil for a disk
	// never published.
	Base    *Generation `json:"base,omitempty"`
	Layers  []layer     `json:"layers"`
	NextSeq int         `json:"next_seq"`
	// HeadFresh is true while the engine created the head empty under the
	// running daemon, so the daemon's write statistics cover all of it.
	HeadFresh bool `json:"head_fresh"`
	// Unformatted is true from creating a new disk until mkfs has written
	// its filesystem, so an attach that stopped between the two formats it
	// next time instead of mounting an empty device.
	Unformatted bool `json:"unformatted,omitempty"`
	// GrowFilesystem is true while the disk has grown and its ext4 has not
	// yet been resized to fill it.
	GrowFilesystem bool            `json:"grow_filesystem,omitempty"`
	Pending        *pendingPublish `json:"pending_publish,omitempty"`
	Collect        *collection     `json:"collect,omitempty"`
	Attachment     *attachment     `json:"attachment,omitempty"`
	LastUsedAt     time.Time       `json:"last_used_at"`
}

func (s *diskState) head() layer { return s.Layers[len(s.Layers)-1] }

func (s *diskState) sealed() []layer { return s.Layers[:len(s.Layers)-1] }

func (s *diskState) newLayer() layer {
	s.NextSeq++
	return layer{Seq: s.NextSeq}
}

// commitPending makes the pending upload the base: the layers it holds
// leave the stack, which returns them for deletion, and what it no longer
// reads awaits collection.
func (s *diskState) commitPending(p diskPaths) ([]layer, error) {
	pending := s.Pending
	if err := os.Rename(p.pendingIndex(), p.baseIndex()); err != nil {
		return nil, fmt.Errorf("keep the index of generation %d: %w", pending.Generation, err)
	}
	var held, kept []layer
	for _, l := range s.Layers {
		if l.Seq <= pending.Through {
			held = append(held, l)
		} else {
			kept = append(kept, l)
		}
	}
	s.Layers = kept
	s.Base = &Generation{Generation: pending.Generation, ManifestKey: pending.ManifestKey, ManifestSHA256: pending.ManifestSHA256}
	var keys []collectKey
	if s.Collect != nil {
		keys = s.Collect.Keys
	}
	s.Collect = &collection{Generation: pending.Generation, Keys: append(keys, pending.Collect...)}
	s.Pending = nil
	return held, nil
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
		return nil, fmt.Errorf("%w: %w: disk %s under %s", ErrInvalid, ErrNoLocalState, p.id, p.root)
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
