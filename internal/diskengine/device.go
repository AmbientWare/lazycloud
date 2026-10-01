package diskengine

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

const (
	sysBlock = "/sys/block"
	// nbdDisconnectTimeout bounds waiting for the kernel to drop a device.
	nbdDisconnectTimeout = 10 * time.Second
)

func deviceName(device string) string { return filepath.Base(device) }

func nbdConnected(device string) bool {
	_, err := os.Stat(filepath.Join(sysBlock, deviceName(device), "pid"))
	return err == nil
}

func nbdSizeBytes(device string) (int64, error) {
	raw, err := os.ReadFile(filepath.Join(sysBlock, deviceName(device), "size")) //nolint:gosec // A sysfs entry.
	if err != nil {
		return 0, fmt.Errorf("read size of %s: %w", device, err)
	}
	sectors, err := strconv.ParseInt(strings.TrimSpace(string(raw)), 10, 64)
	if err != nil {
		return 0, fmt.Errorf("parse size of %s: %w", device, err)
	}
	return sectors * 512, nil
}

// nbdDevices lists the host's nbd devices in index order.
func nbdDevices() ([]string, error) {
	entries, err := os.ReadDir(sysBlock)
	if err != nil {
		return nil, fmt.Errorf("list %s: %w", sysBlock, err)
	}
	type indexed struct {
		name  string
		index int
	}
	var found []indexed
	for _, entry := range entries {
		rest, ok := strings.CutPrefix(entry.Name(), "nbd")
		if !ok {
			continue
		}
		index, err := strconv.Atoi(rest)
		if err != nil {
			continue
		}
		found = append(found, indexed{entry.Name(), index})
	}
	slices.SortFunc(found, func(a, b indexed) int { return a.index - b.index })
	names := make([]string, len(found))
	for i, entry := range found {
		names[i] = entry.name
	}
	return names, nil
}

// claimedDevices lists the devices disks under root record, attached or left
// behind by an agent that died, so none is handed out twice before recover
// has released it.
func claimedDevices(root string) (map[string]bool, error) {
	claimed := map[string]bool{}
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, fmt.Errorf("list %s: %w", root, err)
	}
	for _, entry := range entries {
		if !entry.IsDir() || !diskIDPattern.MatchString(entry.Name()) {
			continue
		}
		state, err := loadState(diskPaths{root: root, id: entry.Name()})
		if err != nil {
			return nil, err
		}
		if state != nil && state.Attachment != nil && state.Attachment.Device != "" {
			claimed[deviceName(state.Attachment.Device)] = true
		}
	}
	return claimed, nil
}

// connectNBD picks a free /dev/nbdN and connects it to the daemon's export.
// Selection and connection happen under the host's NBD lock, and a connected
// device shows a pid in sysfs, so two attaches never pick the same device.
// record saves the device before connecting, so recover finds it to
// disconnect after a crash between the two.
func connectNBD(ctx context.Context, p diskPaths, sizeBytes int64, record func(device string) error) (string, error) {
	lock, err := lockFile(ctx, p.nbdLockPath())
	if err != nil {
		return "", err
	}
	defer lock.release()

	claimed, err := claimedDevices(p.root)
	if err != nil {
		return "", err
	}
	names, err := nbdDevices()
	if err != nil {
		return "", err
	}
	if len(names) == 0 {
		return "", fmt.Errorf("no nbd devices under %s; the nbd module was loaded with nbds_max=0", sysBlock)
	}
	for _, name := range names {
		device := "/dev/" + name
		if claimed[name] || nbdConnected(device) {
			continue
		}
		if size, err := nbdSizeBytes(device); err != nil || size != 0 {
			continue
		}
		if _, err := os.Stat(device); err != nil {
			return "", fmt.Errorf("%s exists in sysfs but not in /dev; mount the host's /dev: %w", device, err)
		}
		if err := record(device); err != nil {
			return "", err
		}
		if _, err := runTool(ctx, toolNBDClient, "-unix", p.nbdSocket(), device,
			"-name", exportName, "-block-size", strconv.Itoa(filesystemBlockBytes)); err != nil {
			return "", err
		}
		size, err := nbdSizeBytes(device)
		if err != nil {
			return "", err
		}
		if size != sizeBytes {
			return "", errors.Join(fmt.Errorf("%s connected with %d bytes, expected %d", device, size, sizeBytes),
				disconnectNBD(context.WithoutCancel(ctx), device))
		}
		return device, nil
	}
	return "", fmt.Errorf("device busy: all %d nbd devices are connected or claimed", len(names))
}

func disconnectNBD(ctx context.Context, device string) error {
	if !nbdConnected(device) {
		return nil
	}
	if _, err := runTool(ctx, toolNBDClient, "-d", device); err != nil {
		return err
	}
	deadline := time.Now().Add(nbdDisconnectTimeout)
	for nbdConnected(device) {
		if time.Now().After(deadline) {
			return fmt.Errorf("%s still connected %s after disconnect", device, nbdDisconnectTimeout)
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("wait for %s to disconnect: %w", device, ctx.Err())
		case <-time.After(50 * time.Millisecond):
		}
	}
	return nil
}

// mountsOf lists this mount namespace's mount points backed by device.
func mountsOf(device string) ([]string, error) {
	var stat unix.Stat_t
	if err := unix.Stat(device, &stat); err != nil {
		return nil, fmt.Errorf("stat %s: %w", device, err)
	}
	want := fmt.Sprintf("%d:%d", unix.Major(stat.Rdev), unix.Minor(stat.Rdev))
	file, err := os.Open("/proc/self/mountinfo")
	if err != nil {
		return nil, fmt.Errorf("open mountinfo: %w", err)
	}
	defer func() { _ = file.Close() }() // Read only.
	var points []string
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) > 4 && fields[2] == want {
			points = append(points, unescapeMountPath(fields[4]))
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("read mountinfo: %w", err)
	}
	return points, nil
}

func unescapeMountPath(path string) string {
	if !strings.Contains(path, `\`) {
		return path
	}
	var out strings.Builder
	for i := 0; i < len(path); i++ {
		if path[i] == '\\' && i+3 < len(path) {
			if value, err := strconv.ParseUint(path[i+1:i+4], 8, 8); err == nil {
				out.WriteByte(byte(value))
				i += 3
				continue
			}
		}
		out.WriteByte(path[i])
	}
	return out.String()
}

func formatExt4(ctx context.Context, device string) error {
	// Inode tables and the journal are written now rather than by the kernel's
	// lazy init thread, which would keep the head changing for hours after the
	// disk is first used and seal a layer every cycle.
	_, err := runTool(ctx, toolMkfs, "-q", "-F", "-b", strconv.Itoa(filesystemBlockBytes),
		"-E", "nodiscard,lazy_itable_init=0,lazy_journal_init=0", device)
	return err
}

func mountExt4(device, mountpoint string) error {
	if err := os.MkdirAll(mountpoint, 0o755); err != nil { //nolint:gosec // Workloads read the mountpoint.
		return fmt.Errorf("create mountpoint %s: %w", mountpoint, err)
	}
	// Online discard turns deleted files into zero clusters in the head, so
	// a flattened layer stops carrying data the filesystem has freed.
	if err := unix.Mount(device, mountpoint, "ext4", unix.MS_NOATIME, "discard"); err != nil {
		return fmt.Errorf("mount %s at %s: %w", device, mountpoint, err)
	}
	return nil
}

// growExt4 resizes a mounted ext4 filesystem to fill its device. Growing
// online needs no fsck first, which an unmounted resize would.
func growExt4(ctx context.Context, device string) error {
	_, err := runTool(ctx, toolResizeFS, device)
	return err
}

func unmount(mountpoint string) error {
	err := unix.Unmount(mountpoint, 0)
	switch {
	case err == nil, errors.Is(err, unix.EINVAL), errors.Is(err, unix.ENOENT):
		return nil
	case errors.Is(err, unix.EBUSY):
		return fmt.Errorf("device busy: %s is still in use: %w", mountpoint, err)
	default:
		return fmt.Errorf("unmount %s: %w", mountpoint, err)
	}
}

// FIFREEZE and FITHAW are _IOWR('X', 119, int) and _IOWR('X', 120, int).
const (
	ioctlFreeze = 0xC0045877
	ioctlThaw   = 0xC0045878
)

func filesystemIoctl(mountpoint string, request uint) error {
	fd, err := unix.Open(mountpoint, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return fmt.Errorf("open %s: %w", mountpoint, err)
	}
	defer func() { _ = unix.Close(fd) }() // A directory opened for an ioctl.
	if err := unix.IoctlSetInt(fd, request, 0); err != nil {
		return fmt.Errorf("ioctl %s: %w", mountpoint, err)
	}
	return nil
}

func freezeFilesystem(mountpoint string) error {
	if err := filesystemIoctl(mountpoint, ioctlFreeze); err != nil {
		return fmt.Errorf("freeze: %w", err)
	}
	return nil
}

func thawFilesystem(mountpoint string) error {
	if err := filesystemIoctl(mountpoint, ioctlThaw); err != nil {
		return fmt.Errorf("thaw: %w", err)
	}
	return nil
}

// thawIfFrozen releases a freeze an interrupted seal left behind. EINVAL is
// the kernel's answer for a filesystem that is not frozen.
func thawIfFrozen(mountpoint string) error {
	err := filesystemIoctl(mountpoint, ioctlThaw)
	if err == nil || errors.Is(err, unix.EINVAL) || errors.Is(err, unix.ENOENT) {
		return nil
	}
	return fmt.Errorf("thaw: %w", err)
}
