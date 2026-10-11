package diskengine

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
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

// createLayer creates an empty qcow2 layer of size bytes. It names no
// backing file: the daemon and qemu-img are always given the layer below.
func createLayer(ctx context.Context, path string, size int64) error {
	_, err := runTool(ctx, toolImage, "create", "-q", "-f", "qcow2", path, strconv.FormatInt(size, 10))
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

var sha256Hex = regexp.MustCompile(`^[0-9a-f]{64}$`)

// served returns the path of a file the snapshotter serves and the device
// of the mount serving it, which a restarted snapshotter changes.
func served(path string) (string, uint64, error) {
	var st unix.Stat_t
	if err := unix.Stat(path, &st); err != nil {
		return "", 0, fmt.Errorf("stat %s: %w", path, err)
	}
	return path, st.Dev, nil
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
