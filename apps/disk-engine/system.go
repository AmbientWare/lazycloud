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
	// -u: the backing file is open read-write in the daemon, and its size is given.
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

// qcow2VirtualSize reads the size a qcow2 image presents from its header:
// the magic "QFI\xfb", then the size as a big-endian uint64 at byte 24.
func qcow2VirtualSize(path string) (int64, error) {
	file, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer file.Close()
	header := make([]byte, 32)
	if _, err := io.ReadFull(file, header); err != nil {
		return 0, fmt.Errorf("read qcow2 header of %s: %w", path, err)
	}
	if !bytes.Equal(header[:4], []byte("QFI\xfb")) {
		return 0, fmt.Errorf("%s is not a qcow2 image", path)
	}
	size := int64(binary.BigEndian.Uint64(header[24:32]))
	if size <= 0 {
		return 0, fmt.Errorf("%s declares a virtual size of %d", path, size)
	}
	return size, nil
}
