// Package diskengine keeps durable disks on a host. A disk is the newest
// generation it published, which the host's snapshotter serves as a
// read-only file whose frames it reads from the workspace bucket on first
// use, under a stack of qcow2 layers in <root>/<disk id>/ holding the
// writes since. A qemu-storage-daemon in a systemd scope of its own, so it
// outlives the call and the process that started it, serves the stack
// through a kernel NBD device mounted as ext4.
//
// Publishing seals the head, reads the frames the sealed layers changed,
// stores those the bucket lacks and writes the next generation's index,
// which names every frame of the disk. The control plane records the
// generation; committing it moves the live stack onto the new generation's
// file and deletes the sealed layers it holds.
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

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

// Errors callers branch on. Others are wrapped failures.
var (
	// ErrInvalid marks a request the engine refuses without touching the
	// disk: a malformed id, size, generation or store, or a call out of
	// order.
	ErrInvalid = errors.New("invalid disk request")
	// ErrNoLocalState marks a disk this host never attached, as after a
	// failed attach: it holds nothing to publish.
	ErrNoLocalState = errors.New("disk has no local state")
	// ErrAttachmentLost marks an attached disk whose daemon, device, mount
	// or served generation had gone. Seal releases what remained and seals
	// what had reached the head, so the disk is detached and its writes
	// publish.
	ErrAttachmentLost = errors.New("disk attachment lost")
)

// Generation is a published generation of a disk: its number and the sha256
// of its stored index, which imagefs.DiskIndexKey names.
type Generation struct {
	Generation  int64  `json:"generation"`
	IndexSHA256 string `json:"index_sha256"`
}

// AttachRequest attaches a disk at Mountpoint.
type AttachRequest struct {
	DiskID    string
	SizeBytes int64
	// Base is the newest published generation; nil for a disk never
	// published, which attaches empty and is formatted.
	Base *Generation
	// Mountpoint is an absolute path, created if missing.
	Mountpoint string
}

// Published is an uploaded generation awaiting CommitPublished.
type Published struct {
	Generation  int64
	IndexSHA256 string
	// AddedBytes counts the stored bytes of the frames this generation
	// added to the bucket.
	AddedBytes int64
}

// LocalDisk is a disk kept under the root.
type LocalDisk struct {
	DiskID   string
	Attached bool
}

// Engine operates the disks under one root directory.
type Engine struct {
	root  string
	bases *layersource.Client
	log   *slog.Logger
}

// New returns an engine for the disks under root, whose published
// generations bases, the host's snapshotter, serves. Unix socket paths
// under root are limited to 107 bytes, so root must be short.
func New(root string, bases *layersource.Client, logger *slog.Logger) *Engine {
	if logger == nil {
		logger = slog.New(slog.DiscardHandler)
	}
	return &Engine{root: filepath.Clean(root), bases: bases, log: logger}
}

const (
	toolDaemon    = "qemu-storage-daemon"
	toolImage     = "qemu-img"
	toolNBDClient = "nbd-client"
	toolMkfs      = "mkfs.ext4"
	toolResizeFS  = "resize2fs"
	toolRunUnit   = "systemd-run"
	sysModuleNBD  = "/sys/module/nbd"
	// systemdRunning exists while systemd is the init system.
	systemdRunning = "/run/systemd/system"
)

// Check reports what this host lacks to attach disks: a required tool, the
// nbd kernel module, systemd or root privileges.
func Check() error {
	var missing []error
	for _, tool := range []string{toolDaemon, toolImage, toolNBDClient, toolMkfs, toolResizeFS, toolRunUnit} {
		if _, err := exec.LookPath(tool); err != nil {
			missing = append(missing, fmt.Errorf("%s is not installed: %w", tool, err))
		}
	}
	if _, err := os.Stat(sysModuleNBD); err != nil {
		missing = append(missing, fmt.Errorf("the nbd kernel module is not loaded (%s is missing); load it at boot with nbds_max=128", sysModuleNBD))
	}
	if _, err := os.Stat(systemdRunning); err != nil {
		missing = append(missing, fmt.Errorf("systemd is not running (%s is missing); each disk's daemon runs in a systemd scope", systemdRunning))
	}
	if uid := os.Geteuid(); uid != 0 {
		missing = append(missing, fmt.Errorf("the disk engine needs root to connect NBD devices and mount, running as uid %d", uid))
	}
	if len(missing) > 0 {
		return fmt.Errorf("host cannot attach disks: %w", errors.Join(missing...))
	}
	return nil
}

func (e *Engine) paths(diskID string) (diskPaths, error) {
	if !imagefs.DiskID(diskID) {
		return diskPaths{}, fmt.Errorf("%w: disk id %q must be 1-128 letters, digits, underscores or hyphens", ErrInvalid, diskID)
	}
	return diskPaths{root: e.root, id: diskID}, nil
}
