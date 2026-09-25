package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
)

const (
	toolDaemon    = "qemu-storage-daemon"
	toolImage     = "qemu-img"
	toolNBDClient = "nbd-client"
	toolMkfs      = "mkfs.ext4"
	toolResizeFS  = "resize2fs"
)

func requireTools(names ...string) error {
	for _, name := range names {
		if _, err := exec.LookPath(name); err != nil {
			return fmt.Errorf("%s is not installed: %w", name, err)
		}
	}
	return nil
}

func runTool(ctx context.Context, name string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, name, args...)
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
	_, err := runTool(ctx, toolImage, "create", "-q", "-f", "qcow2", "-u",
		"-b", backing.file(), "-F", backing.format(), path, fmt.Sprint(size))
	return err
}

func createBase(ctx context.Context, path string, size int64) error {
	_, err := runTool(ctx, toolImage, "create", "-q", "-f", "qcow2", path, fmt.Sprint(size))
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
	return false, err
}

// qcow2HeaderBytes covers every field parseQcow2Header reads.
const qcow2HeaderBytes = 64

// qcow2Header is the part of a qcow2 header the engine reads. Fields are
// big-endian at fixed offsets after the magic "QFI\xfb".
type qcow2Header struct {
	clusterBytes     int64
	virtualSize      int64
	l1Entries        int64
	l1Offset         int64
	refcountOffset   int64
	refcountClusters int64
}

func parseQcow2Header(name string, raw []byte) (qcow2Header, error) {
	if len(raw) < qcow2HeaderBytes || !bytes.Equal(raw[:4], []byte("QFI\xfb")) {
		return qcow2Header{}, fmt.Errorf("%s is not a qcow2 image", name)
	}
	clusterBits := binary.BigEndian.Uint32(raw[20:24])
	header := qcow2Header{
		virtualSize:      int64(binary.BigEndian.Uint64(raw[24:32])),
		l1Entries:        int64(binary.BigEndian.Uint32(raw[36:40])),
		l1Offset:         int64(binary.BigEndian.Uint64(raw[40:48])),
		refcountOffset:   int64(binary.BigEndian.Uint64(raw[48:56])),
		refcountClusters: int64(binary.BigEndian.Uint32(raw[56:60])),
	}
	if clusterBits < 9 || clusterBits > 21 {
		return header, fmt.Errorf("%s has %d-bit clusters", name, clusterBits)
	}
	header.clusterBytes = int64(1) << clusterBits
	if header.virtualSize <= 0 {
		return header, fmt.Errorf("%s declares a virtual size of %d", name, header.virtualSize)
	}
	return header, nil
}

// qcow2VirtualSize reads the size a qcow2 image presents from its header.
func qcow2VirtualSize(path string) (int64, error) {
	file, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer file.Close()
	raw := make([]byte, qcow2HeaderBytes)
	if _, err := io.ReadFull(file, raw); err != nil {
		return 0, fmt.Errorf("read qcow2 header of %s: %w", path, err)
	}
	header, err := parseQcow2Header(path, raw)
	return header.virtualSize, err
}
