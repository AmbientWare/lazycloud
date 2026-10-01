// Package diskengine keeps durable disks on a host. Each disk is a qcow2
// layer chain under <root>/<disk id>/, served by a qemu-storage-daemon that
// outlives the call that started it, exposed through a kernel NBD device and
// mounted as ext4. Sealed layers publish to the workspace bucket as
// content-defined chunks plus a manifest; the control plane records each
// published generation and the engine commits it once that record exists.
//
// Every operation on a disk holds a flock outside the disk's directory, so
// calls from several goroutines or processes serialize per disk. One root
// serves a whole host.
package diskengine

import (
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"time"
)

// Errors callers branch on. Others are wrapped failures.
var (
	// ErrInvalid marks a request the engine refuses without touching the
	// disk: a malformed id, size, chain or store, or a call out of order.
	ErrInvalid = errors.New("invalid disk request")
	// ErrInsufficientSpace marks a restore that does not fit under the root.
	// The error is an *InsufficientSpaceError naming the shortfall; evicting
	// cached disks and retrying may succeed.
	ErrInsufficientSpace = errors.New("insufficient space")
	// ErrNotAttached marks an operation that needs the disk mounted here.
	ErrNotAttached = errors.New("disk is not attached")
	// ErrNoLocalState marks a disk this host never restored, as after a
	// failed attach: it holds nothing to publish.
	ErrNoLocalState = errors.New("disk has no local state")
	// ErrCredentialsExpired marks store credentials the callback returned
	// already expired.
	ErrCredentialsExpired = errors.New("storage credentials expired")
)

// InsufficientSpaceError says how far a restore is from fitting.
type InsufficientSpaceError struct {
	Root    string
	Need    int64
	Have    int64
	Reserve int64
}

func (e *InsufficientSpaceError) Error() string {
	return fmt.Sprintf("insufficient space on %s: need %d, have %d free, reserve %d", e.Root, e.Need, e.Have, e.Reserve)
}

// Is matches ErrInsufficientSpace.
func (e *InsufficientSpaceError) Is(target error) bool { return target == ErrInsufficientSpace }

// Shortfall is how many more free bytes the restore needs.
func (e *InsufficientSpaceError) Shortfall() int64 { return max(e.Need+e.Reserve-e.Have, 0) }

// Generation is one published generation in a disk's chain.
type Generation struct {
	Generation     int64
	ManifestKey    string
	ManifestSHA256 string
}

// AttachRequest attaches a disk at Mountpoint. Chain lists the published
// generations to restore, base first; it is empty for a disk never published.
type AttachRequest struct {
	DiskID    string
	SizeBytes int64
	Chain     []Generation
	// Mountpoint is an absolute path, created if missing.
	Mountpoint string
	Store      Store
	// MinFreeBytes is the space the filesystem under the root must keep free
	// after a restore.
	MinFreeBytes int64
}

// AttachResult describes an attached disk.
type AttachResult struct {
	// Generation is the newest published generation the disk holds.
	Generation int64
	// Reused is true when the local copy already held Generation.
	Reused bool
	// Formatted is true when this attach created the filesystem.
	Formatted bool
}

// Published is an uploaded layer awaiting CommitPublished. ParentGeneration
// is 0 for a self-contained generation, which Flat marks when the engine
// flattened the chain into it.
type Published struct {
	Generation       int64
	ParentGeneration int64
	ManifestKey      string
	ManifestSHA256   string
	// AddedBytes counts chunk bytes this upload stored that the bucket did
	// not hold before.
	AddedBytes int64
	Flat       bool
}

// LocalDisk is a disk kept under the root.
type LocalDisk struct {
	DiskID     string
	Attached   bool
	LocalBytes int64
	LastUsedAt time.Time
	// Unpublished is true while the disk may hold writes no committed
	// generation contains, so evicting it would lose them.
	Unpublished bool
}

// Engine operates the disks under one root directory.
type Engine struct {
	root string
	log  *slog.Logger
}

// New returns an engine for the disks under root. Unix socket paths under
// root are limited to 107 bytes, so root must be short.
func New(root string, logger *slog.Logger) *Engine {
	if logger == nil {
		logger = slog.New(slog.DiscardHandler)
	}
	return &Engine{root: filepath.Clean(root), log: logger}
}

const (
	toolDaemon    = "qemu-storage-daemon"
	toolImage     = "qemu-img"
	toolNBDClient = "nbd-client"
	toolMkfs      = "mkfs.ext4"
	toolResizeFS  = "resize2fs"
	sysModuleNBD  = "/sys/module/nbd"
)

// Check reports what this host lacks to attach disks: a required tool, the
// nbd kernel module or root privileges.
func (e *Engine) Check() error {
	var missing []error
	for _, tool := range []string{toolDaemon, toolImage, toolNBDClient, toolMkfs, toolResizeFS} {
		if _, err := exec.LookPath(tool); err != nil {
			missing = append(missing, fmt.Errorf("%s is not installed: %w", tool, err))
		}
	}
	if _, err := os.Stat(sysModuleNBD); err != nil {
		missing = append(missing, fmt.Errorf("the nbd kernel module is not loaded (%s is missing); load it at boot with nbds_max=128", sysModuleNBD))
	}
	if uid := os.Geteuid(); uid != 0 {
		missing = append(missing, fmt.Errorf("the disk engine needs root to connect NBD devices and mount, running as uid %d", uid))
	}
	if len(missing) > 0 {
		return fmt.Errorf("host cannot attach disks: %w", errors.Join(missing...))
	}
	return nil
}

var diskIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)

func (e *Engine) paths(diskID string) (diskPaths, error) {
	if !diskIDPattern.MatchString(diskID) {
		return diskPaths{}, fmt.Errorf("%w: disk id %q must be 1-128 letters, digits, underscores or hyphens", ErrInvalid, diskID)
	}
	return diskPaths{root: e.root, id: diskID}, nil
}
