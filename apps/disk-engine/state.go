package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"time"

	"golang.org/x/sys/unix"
)

var diskIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)

func validateDiskID(id string) error {
	if !diskIDPattern.MatchString(id) {
		return fmt.Errorf("disk id %q must be 1-128 letters, digits, underscores or hyphens", id)
	}
	return nil
}

// maxSocketPath is sun_path's capacity less the terminating NUL.
const maxSocketPath = 107

type diskPaths struct {
	root string
	id   string
}

func (p diskPaths) dir() string           { return filepath.Join(p.root, p.id) }
func (p diskPaths) statePath() string     { return filepath.Join(p.dir(), "state.json") }
func (p diskPaths) layerDir() string      { return filepath.Join(p.dir(), "layers") }
func (p diskPaths) runDir() string        { return filepath.Join(p.dir(), "run") }
func (p diskPaths) qmpSocket() string     { return filepath.Join(p.runDir(), "qmp.sock") }
func (p diskPaths) nbdSocket() string     { return filepath.Join(p.runDir(), "nbd.sock") }
func (p diskPaths) pidFile() string       { return filepath.Join(p.runDir(), "qsd.pid") }
func (p diskPaths) pointPath() string     { return filepath.Join(p.dir(), "snapshot.json") }
func (p diskPaths) heatPath() string      { return filepath.Join(p.dir(), "heat") }
func (p diskPaths) hydrateLog() string    { return filepath.Join(p.runDir(), "hydrate.log") }
func (p diskPaths) hydrateStatus() string { return filepath.Join(p.runDir(), "hydrate.json") }
func (p diskPaths) layerPath(l layer) string {
	return filepath.Join(p.layerDir(), l.file())
}

// Locks live outside the disk directory so evicting a disk cannot hand a
// waiting process a lock on an unlinked file while a new directory appears.
func (p diskPaths) lockPath() string { return filepath.Join(p.root, ".locks", p.id) }

func (p diskPaths) checkSocketPaths() error {
	for _, path := range []string{p.qmpSocket(), p.nbdSocket()} {
		if len(path) > maxSocketPath {
			return fmt.Errorf("socket path %s is %d bytes, over the %d a unix socket allows; use a shorter --root", path, len(path), maxSocketPath)
		}
	}
	return nil
}

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

const (
	formatQcow2 = "qcow2"
	formatRaw   = "raw"
)

func (l layer) format() string {
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

// pendingPublish is an upload whose generation the control plane has not yet
// confirmed. A retried publish of the same layer and generation returns it
// unchanged, so stored_bytes_added is not lost to chunks the first attempt
// already stored.
type pendingPublish struct {
	Seq    int           `json:"seq"`
	Flat   bool          `json:"flatten"`
	Result publishResult `json:"result"`
}

// publishedRecord is a generation this node knows the control plane recorded,
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
	// GrowFilesystem is true while the disk has grown and its ext4 has not
	// yet been resized to fill it.
	GrowFilesystem          bool              `json:"grow_filesystem,omitempty"`
	PublishedGeneration     int64             `json:"published_generation"`
	PublishedManifestSHA256 string            `json:"published_manifest_sha256"`
	Published               []publishedRecord `json:"published"`
	Pending                 *pendingPublish   `json:"pending_publish,omitempty"`
	Attachment              *attachment       `json:"attachment,omitempty"`
	LastUsedAt              time.Time         `json:"last_used_at"`
	// AdoptedPoint is the snapshot point this state was taken from, so an
	// attach retried on a volume made from a snapshot does not adopt it twice.
	AdoptedPoint string `json:"adopted_point,omitempty"`
	// Hydrating is true from adopting a snapshot until a hydrator has read
	// every block of the published layers once.
	Hydrating   bool `json:"hydrating,omitempty"`
	HydratorPID int  `json:"hydrator_pid,omitempty"`
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

// publishedPrefix is how many layers from the base hold published generations.
func (s *diskState) publishedPrefix() int {
	count := 0
	for count < len(s.Layers)-1 && s.Layers[count].Generation > 0 {
		count++
	}
	return count
}

func (s *diskState) oldestUnpublished() int {
	for i := 0; i < len(s.Layers)-1; i++ {
		if s.Layers[i].Generation == 0 {
			return i
		}
	}
	return -1
}

func (s *diskState) unpublishedSealed() int {
	count := 0
	for i := 0; i < len(s.Layers)-1; i++ {
		if s.Layers[i].Generation == 0 {
			count++
		}
	}
	return count
}

func loadState(p diskPaths) (*diskState, error) {
	data, err := os.ReadFile(p.statePath())
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
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
		return nil, fmt.Errorf("disk %s has no local state under %s", p.id, p.root)
	}
	return state, nil
}

func saveState(p diskPaths, state *diskState) error {
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	return writeFileAtomic(p.statePath(), data)
}

func writeFileAtomic(path string, data []byte) error {
	tmp := path + ".tmp"
	file, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, err := file.Write(data); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		return err
	}
	return syncDir(filepath.Dir(path))
}

func syncDir(path string) error {
	dir, err := os.Open(path)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

type fileLock struct{ file *os.File }

func lockFile(path string) (*fileLock, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, err
	}
	if err := unix.Flock(int(file.Fd()), unix.LOCK_EX); err != nil {
		file.Close()
		return nil, fmt.Errorf("lock %s: %w", path, err)
	}
	return &fileLock{file: file}, nil
}

// tryLockFile returns nil without waiting when another process holds the lock.
func tryLockFile(path string) (*fileLock, error) {
	file, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, err
	}
	if err := unix.Flock(int(file.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		file.Close()
		if errors.Is(err, unix.EWOULDBLOCK) {
			return nil, nil
		}
		return nil, fmt.Errorf("lock %s: %w", path, err)
	}
	return &fileLock{file: file}, nil
}

func (l *fileLock) release() {
	unix.Flock(int(l.file.Fd()), unix.LOCK_UN)
	l.file.Close()
}

func lockDisk(p diskPaths) (*fileLock, error) {
	if err := os.MkdirAll(p.root, 0o700); err != nil {
		return nil, err
	}
	return lockFile(p.lockPath())
}
