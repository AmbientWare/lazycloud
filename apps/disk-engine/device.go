package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

const (
	sysModuleNBD = "/sys/module/nbd"
	sysBlock     = "/sys/block"
)

func requireNBDModule() error {
	if _, err := os.Stat(sysModuleNBD); err != nil {
		return fmt.Errorf("the nbd kernel module is not loaded on this host (%s is missing); nodes load it at boot with nbds_max=128", sysModuleNBD)
	}
	return nil
}

func deviceName(device string) string { return filepath.Base(device) }

func nbdConnected(device string) bool {
	_, err := os.Stat(filepath.Join(sysBlock, deviceName(device), "pid"))
	return err == nil
}

func nbdSizeBytes(device string) (int64, error) {
	raw, err := os.ReadFile(filepath.Join(sysBlock, deviceName(device), "size"))
	if err != nil {
		return 0, err
	}
	sectors, err := strconv.ParseInt(strings.TrimSpace(string(raw)), 10, 64)
	if err != nil {
		return 0, err
	}
	return sectors * 512, nil
}

func nbdDevices() ([]string, error) {
	entries, err := os.ReadDir(sysBlock)
	if err != nil {
		return nil, err
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
	sort.Slice(found, func(i, j int) bool { return found[i].index < found[j].index })
	names := make([]string, len(found))
	for i, entry := range found {
		names[i] = entry.name
	}
	return names, nil
}

// claimedDevices lists the devices other disks under root record, attached or
// left behind by a worker that died, so none is handed out twice before
// recover has released it. Claims under /run cover every root but do not
// survive a restart of the worker's container. State under root does.
func claimedDevices(root string) (map[string]bool, error) {
	claimed := map[string]bool{}
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, err
	}
	for _, entry := range entries {
		if !entry.IsDir() || validateDiskID(entry.Name()) != nil {
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

// nbdLockPath serializes device selection across every root, since a disk
// on its own volume has a root of its own.
const nbdLockPath = "/run/lazycloud-disk/nbd.lock"

// nbdClaimDir holds one file per device a disk has recorded, naming the disk,
// so a device recorded under any root is taken for every other root. A claim
// is dropped when its disk detaches, or found stale when that disk's state no
// longer records the device. A disk whose state cannot be read keeps its
// claim, because its root may be a volume that is not mounted again yet.
const nbdClaimDir = "/run/lazycloud-disk/claims"

type deviceClaim struct {
	Root string `json:"root"`
	Disk string `json:"disk"`
}

func claimPath(device string) string { return filepath.Join(nbdClaimDir, deviceName(device)) }

func writeClaim(device string, p diskPaths) error {
	if err := os.MkdirAll(nbdClaimDir, 0o700); err != nil {
		return err
	}
	data, err := json.Marshal(deviceClaim{Root: p.root, Disk: p.id})
	if err != nil {
		return err
	}
	return writeFileAtomic(claimPath(device), data)
}

// releaseClaim drops the claim on device if it is this disk's.
func releaseClaim(device string, p diskPaths) error {
	var claim deviceClaim
	if err := readJSONFile(claimPath(device), &claim); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	if claim.Root != p.root || claim.Disk != p.id {
		return nil
	}
	if err := os.Remove(claimPath(device)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// claimTaken reports whether another disk holds device. A claim whose disk
// state is readable and records a different device, or none, is stale and
// removed.
func claimTaken(device string) (bool, error) {
	var claim deviceClaim
	if err := readJSONFile(claimPath(device), &claim); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return false, nil
		}
		return false, err
	}
	state, err := loadState(diskPaths{root: claim.Root, id: claim.Disk})
	if err != nil || state == nil {
		return true, nil
	}
	if state.Attachment != nil && state.Attachment.Device == device {
		return true, nil
	}
	if err := os.Remove(claimPath(device)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return false, err
	}
	return false, nil
}

// connectNBD picks a free /dev/nbdN and connects it to the daemon's export.
// Selection and connection happen under one lock, and a connected device
// shows a pid in sysfs, so two attaches never pick the same device.
func connectNBD(ctx context.Context, p diskPaths, sizeBytes int64, record func(device string) error) (string, error) {
	lock, err := lockFile(nbdLockPath)
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
		taken, err := claimTaken(device)
		if err != nil {
			return "", err
		}
		if taken {
			continue
		}
		if size, err := nbdSizeBytes(device); err != nil || size != 0 {
			continue
		}
		if _, err := os.Stat(device); err != nil {
			return "", fmt.Errorf("%s exists in sysfs but not in /dev; mount the host's /dev into the worker: %w", device, err)
		}
		// Claim and record the device before connecting. After a crash between
		// the two, recover finds it to disconnect and no other root can take it.
		if err := writeClaim(device, p); err != nil {
			return "", err
		}
		if err := record(device); err != nil {
			return "", err
		}
		if _, err := runTool(ctx, toolNBDClient, "-unix", p.nbdSocket(), device,
			"-name", exportName, "-block-size", "4096"); err != nil {
			return "", err
		}
		size, err := nbdSizeBytes(device)
		if err != nil {
			return "", err
		}
		if size != sizeBytes {
			disconnectNBD(context.WithoutCancel(ctx), device)
			return "", fmt.Errorf("%s connected with %d bytes, expected %d", device, size, sizeBytes)
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
	deadline := time.Now().Add(10 * time.Second)
	for nbdConnected(device) {
		if time.Now().After(deadline) {
			return fmt.Errorf("%s still connected 10s after disconnect", device)
		}
		time.Sleep(50 * time.Millisecond)
	}
	return nil
}

// mountsOf lists this mount namespace's mount points backed by device.
func mountsOf(device string) ([]string, error) {
	var stat unix.Stat_t
	if err := unix.Stat(device, &stat); err != nil {
		return nil, fmt.Errorf("stat %s: %w", device, err)
	}
	want := fmt.Sprintf("%d:%d", unix.Major(uint64(stat.Rdev)), unix.Minor(uint64(stat.Rdev)))
	file, err := os.Open("/proc/self/mountinfo")
	if err != nil {
		return nil, err
	}
	defer file.Close()
	var points []string
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) > 4 && fields[2] == want {
			points = append(points, unescapeMountPath(fields[4]))
		}
	}
	return points, scanner.Err()
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
	_, err := runTool(ctx, toolMkfs, "-q", "-F", "-b", fmt.Sprint(filesystemBlockBytes),
		"-E", "nodiscard,lazy_itable_init=0,lazy_journal_init=0", device)
	return err
}

func mountExt4(device, mountpoint string) error {
	if err := os.MkdirAll(mountpoint, 0o755); err != nil {
		return err
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
		return fmt.Errorf("device busy: %s is still in use", mountpoint)
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
		return err
	}
	defer unix.Close(fd)
	return unix.IoctlSetInt(fd, request, 0)
}

func freezeFilesystem(mountpoint string) error {
	if err := filesystemIoctl(mountpoint, ioctlFreeze); err != nil {
		return fmt.Errorf("freeze %s: %w", mountpoint, err)
	}
	return nil
}

func thawFilesystem(mountpoint string) error {
	if err := filesystemIoctl(mountpoint, ioctlThaw); err != nil {
		return fmt.Errorf("thaw %s: %w", mountpoint, err)
	}
	return nil
}

// thawIfFrozen releases a freeze a killed seal left behind. EINVAL is the
// kernel's answer for a filesystem that is not frozen.
func thawIfFrozen(mountpoint string) error {
	err := filesystemIoctl(mountpoint, ioctlThaw)
	if err == nil || errors.Is(err, unix.EINVAL) || errors.Is(err, unix.ENOENT) {
		return nil
	}
	return fmt.Errorf("thaw %s: %w", mountpoint, err)
}
