package diskengine

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

func runTool(ctx context.Context, name string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, name, args...) //nolint:gosec // The engine names every tool and builds its arguments.
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		var execErr *exec.Error
		if errors.As(err, &execErr) {
			return "", fmt.Errorf("%s is not installed: %w", name, err)
		}
		detail := strings.TrimSpace(stderr.String())
		if detail == "" {
			detail = strings.TrimSpace(stdout.String())
		}
		return "", fmt.Errorf("%s %s: %w: %s", name, strings.Join(args, " "), err, detail)
	}
	return stdout.String(), nil
}

func createOverlay(ctx context.Context, path string, backing layer, size int64) error {
	// -u skips opening the backing file, which the daemon holds read-write, so
	// the size is passed explicitly.
	_, err := runTool(ctx, toolImage, "create", "-q", "-f", string(formatQcow2), "-u",
		"-b", backing.file(), "-F", string(backing.format()), path, strconv.FormatInt(size, 10))
	return err
}

func createBase(ctx context.Context, path string, size int64) error {
	_, err := runTool(ctx, toolImage, "create", "-q", "-f", string(formatQcow2), path, strconv.FormatInt(size, 10))
	return err
}

func pathExists(path string) (bool, error) {
	_, err := os.Stat(path)
	if err == nil {
		return true, nil
	}
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	return false, fmt.Errorf("stat %s: %w", path, err)
}

func removeIfExists(path string) error {
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("remove %s: %w", path, err)
	}
	return nil
}

// qcow2HeaderBytes covers every field the engine reads.
const qcow2HeaderBytes = 64

// qcow2VirtualSize reads the size a qcow2 image presents from its header.
// Fields are big-endian at fixed offsets after the magic "QFI\xfb".
func qcow2VirtualSize(path string) (int64, error) {
	file, err := os.Open(path) //nolint:gosec // A layer path under the root.
	if err != nil {
		return 0, fmt.Errorf("open %s: %w", path, err)
	}
	defer func() { _ = file.Close() }() // Read only.
	raw := make([]byte, qcow2HeaderBytes)
	if _, err := io.ReadFull(file, raw); err != nil {
		return 0, fmt.Errorf("read qcow2 header of %s: %w", path, err)
	}
	if !bytes.Equal(raw[:4], []byte("QFI\xfb")) {
		return 0, fmt.Errorf("%s is not a qcow2 image", path)
	}
	clusterBits := binary.BigEndian.Uint32(raw[20:24])
	if clusterBits < 9 || clusterBits > 21 {
		return 0, fmt.Errorf("%s has %d-bit clusters", path, clusterBits)
	}
	size := int64(binary.BigEndian.Uint64(raw[24:32])) //nolint:gosec // Checked positive below.
	if size <= 0 {
		return 0, fmt.Errorf("%s declares a virtual size of %d", path, size)
	}
	return size, nil
}

// layerVirtualSize is the size of the disk a layer presents.
func layerVirtualSize(path string, l layer) (int64, error) {
	if l.Raw {
		info, err := os.Stat(path)
		if err != nil {
			return 0, fmt.Errorf("stat %s: %w", path, err)
		}
		return info.Size(), nil
	}
	return qcow2VirtualSize(path)
}

// allocatedBytes is the disk space the files under dir occupy, holes excluded.
func allocatedBytes(dir string) (int64, error) {
	var total int64
	err := filepath.WalkDir(dir, func(path string, _ fs.DirEntry, err error) error {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		if err != nil {
			return err
		}
		var stat unix.Stat_t
		if err := unix.Lstat(path, &stat); err != nil {
			if errors.Is(err, unix.ENOENT) {
				return nil
			}
			return fmt.Errorf("stat %s: %w", path, err)
		}
		total += stat.Blocks * 512
		return nil
	})
	if err != nil {
		return 0, fmt.Errorf("measure %s: %w", dir, err)
	}
	return total, nil
}

// freeBytes is the space the filesystem under the root has for this disk.
// Space a restore frees by replacing this disk's stale local copy counts as
// free.
func freeBytes(p diskPaths) (int64, error) {
	var stat unix.Statfs_t
	if err := unix.Statfs(p.root, &stat); err != nil {
		return 0, fmt.Errorf("statfs %s: %w", p.root, err)
	}
	have := int64(stat.Bavail) * stat.Bsize //nolint:gosec // Block counts fit an int64.
	stale, err := allocatedBytes(p.dir())
	if err != nil {
		return 0, err
	}
	return have + stale, nil
}

// processArgs is pid's command line, nil when no such process runs.
func processArgs(pid int) []string {
	if pid <= 0 {
		return nil
	}
	cmdline, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "cmdline")) //nolint:gosec // A /proc entry.
	if err != nil || len(cmdline) == 0 {
		return nil
	}
	return strings.Split(strings.TrimRight(string(cmdline), "\x00"), "\x00")
}
