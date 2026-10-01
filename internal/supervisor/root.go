package supervisor

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

// seededMarker, inside a devbox root, records that seeding finished; a seed
// cut short is redone at the next start.
const seededMarker = "var/lib/lazycloud/root-seeded"

// rootBinds are bound into a devbox root even when they are not mount
// points; every other mount point is bound too.
var rootBinds = []string{"/proc", "/dev", "/sys", "/run/lazycloud", "/etc/hosts", "/etc/resolv.conf"} //nolint:gochecknoglobals // constant list

// enterRoot makes root the supervisor's /: it seeds root from the image on
// first use, binds the container's mounts into it, then chroots there, so
// the command and every process, shell and SSH session run in it.
func enterRoot(root string) error {
	root = filepath.Clean(root)
	if !filepath.IsAbs(root) || root == "/" {
		return fmt.Errorf("the devbox root %q must be an absolute directory other than /", root)
	}
	mounts, err := readMounts()
	if err != nil {
		return err
	}
	skip := mountSet(mounts)
	skip[root] = true
	if _, err := os.Stat(filepath.Join(root, seededMarker)); errors.Is(err, fs.ErrNotExist) {
		started := time.Now()
		if err := seedRoot(root, skip); err != nil {
			return err
		}
		if err := os.MkdirAll(filepath.Dir(filepath.Join(root, seededMarker)), 0o755); err != nil { //nolint:gosec // a system directory
			return fmt.Errorf("record the seeded root: %w", err)
		}
		if err := os.WriteFile(filepath.Join(root, seededMarker), nil, 0o644); err != nil { //nolint:gosec // see above
			return fmt.Errorf("record the seeded root: %w", err)
		}
		fmt.Fprintf(os.Stderr, "seeded the devbox root %s in %s\n", root, time.Since(started).Round(time.Millisecond))
	} else if err != nil {
		return fmt.Errorf("check the devbox root: %w", err)
	}
	for _, m := range rootMounts(mounts, root) {
		if err := bindInto(root, m); err != nil {
			return err
		}
	}
	if err := unix.Chroot(root); err != nil {
		return fmt.Errorf("chroot to %s: %w", root, err)
	}
	if err := os.Chdir("/"); err != nil {
		return fmt.Errorf("enter the devbox root: %w", err)
	}
	return nil
}

// dropMountPrivilege takes CAP_SYS_ADMIN, which only entering the root
// needed, out of reach of every process the devbox runs: it leaves the
// bounding and ambient sets of every supervisor thread, so no exec regains
// it, and no_new_privs stops set-id and file-capability programs adding it
// back. The supervisor keeps it in its own permitted set. Docker grants no
// inheritable capabilities; one that did would pass it to children, so that
// refuses the start.
func dropMountPrivilege() error {
	for _, call := range []struct {
		what string
		args [3]uintptr
	}{
		{"drop CAP_SYS_ADMIN from the bounding set", [3]uintptr{unix.PR_CAPBSET_DROP, unix.CAP_SYS_ADMIN, 0}},
		{"clear the ambient capabilities", [3]uintptr{unix.PR_CAP_AMBIENT, unix.PR_CAP_AMBIENT_CLEAR_ALL, 0}},
		{"set no_new_privs", [3]uintptr{unix.PR_SET_NO_NEW_PRIVS, 1, 0}},
	} {
		if _, _, errno := syscall.AllThreadsSyscall(unix.SYS_PRCTL, call.args[0], call.args[1], call.args[2]); errno != 0 {
			return fmt.Errorf("%s: %w", call.what, errno)
		}
	}
	header := unix.CapUserHeader{Version: unix.LINUX_CAPABILITY_VERSION_3}
	var sets [2]unix.CapUserData
	if err := unix.Capget(&header, &sets[0]); err != nil {
		return fmt.Errorf("read capabilities: %w", err)
	}
	if sets[0].Inheritable&(1<<unix.CAP_SYS_ADMIN) != 0 {
		return errors.New("the container grants CAP_SYS_ADMIN as inheritable, which would reach the devbox's processes")
	}
	return nil
}

// rootMounts are the mount points to bind into root: the top-level ones,
// whose submounts a recursive bind carries, and rootBinds, outside root.
func rootMounts(mounts []mountPoint, root string) []mountPoint {
	under := func(path, dir string) bool { return path != dir && strings.HasPrefix(path, dir+"/") }
	candidates := slices.Clone(mounts)
	for _, path := range rootBinds {
		if !slices.ContainsFunc(candidates, func(m mountPoint) bool { return m.path == path }) {
			if _, err := os.Lstat(path); err == nil {
				candidates = append(candidates, mountPoint{path: path})
			}
		}
	}
	var out []mountPoint
	for _, m := range candidates {
		if m.path == "/" || m.path == root || under(m.path, root) || under(root, m.path) {
			continue
		}
		nested := slices.ContainsFunc(candidates, func(parent mountPoint) bool {
			return parent.path != "/" && under(m.path, parent.path)
		})
		if !nested && !slices.ContainsFunc(out, func(o mountPoint) bool { return o.path == m.path }) {
			out = append(out, m)
		}
	}
	return out
}

// bindInto binds m recursively at the same path under root, with its
// read-only and nosuid, nodev and noexec flags.
func bindInto(root string, m mountPoint) error {
	target := filepath.Join(root, m.path)
	info, err := os.Stat(m.path)
	if err != nil {
		return fmt.Errorf("stat mount %s: %w", m.path, err)
	}
	if info.IsDir() {
		err = os.MkdirAll(target, 0o755) //nolint:gosec // a mount point
	} else if err = os.MkdirAll(filepath.Dir(target), 0o755); err == nil { //nolint:gosec // see above
		var f *os.File
		if f, err = os.OpenFile(target, os.O_CREATE|os.O_WRONLY, 0o644); err == nil { //nolint:gosec // see above
			err = f.Close()
		}
	}
	if err != nil {
		return fmt.Errorf("create mount point %s: %w", target, err)
	}
	if err := unix.Mount(m.path, target, "", unix.MS_BIND|unix.MS_REC, ""); err != nil {
		return fmt.Errorf("bind %s into the devbox root: %w", m.path, err)
	}
	var flags uintptr
	for _, option := range m.options {
		switch option {
		case "ro":
			flags |= unix.MS_RDONLY
		case "nosuid":
			flags |= unix.MS_NOSUID
		case "nodev":
			flags |= unix.MS_NODEV
		case "noexec":
			flags |= unix.MS_NOEXEC
		}
	}
	if flags != 0 {
		if err := unix.Mount("", target, "", unix.MS_BIND|unix.MS_REMOUNT|flags, ""); err != nil {
			return fmt.Errorf("remount %s in the devbox root: %w", m.path, err)
		}
	}
	return nil
}

// seedRoot copies the image's root filesystem into root, except mount
// points and root itself, keeping modes, owners, links, extended attributes
// and modification times.
func seedRoot(root string, skip map[string]bool) error {
	hardlinks := make(map[[2]uint64]string)
	type dirTimes struct {
		path  string
		mtime time.Time
	}
	var dirs []dirTimes
	err := filepath.WalkDir("/", func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			if errors.Is(walkErr, fs.ErrNotExist) {
				return nil
			}
			return walkErr
		}
		if path == "/" {
			return nil
		}
		if skip[path] {
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		info, err := entry.Info()
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("stat %s: %w", path, err)
		}
		target := filepath.Join(root, path)
		if err := copyEntry(path, target, info, hardlinks); err != nil {
			return err
		}
		if info.IsDir() {
			dirs = append(dirs, dirTimes{target, info.ModTime()})
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("seed the devbox root: %w", err)
	}
	// Directory times last, since filling a directory changes its time.
	for i := len(dirs) - 1; i >= 0; i-- {
		_ = os.Chtimes(dirs[i].path, dirs[i].mtime, dirs[i].mtime)
	}
	return nil
}

func copyEntry(path, target string, info fs.FileInfo, hardlinks map[[2]uint64]string) error {
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return fmt.Errorf("stat %s: no file status", path)
	}
	mode := info.Mode()
	if !mode.IsDir() {
		if err := os.Remove(target); err != nil && !errors.Is(err, fs.ErrNotExist) {
			return fmt.Errorf("replace %s: %w", target, err)
		}
	}
	switch {
	case mode.IsDir():
		if err := os.Mkdir(target, 0o700); err != nil && !errors.Is(err, fs.ErrExist) {
			return fmt.Errorf("create %s: %w", target, err)
		}
	case mode&os.ModeSymlink != 0:
		link, err := os.Readlink(path)
		if err != nil {
			return fmt.Errorf("read link %s: %w", path, err)
		}
		if err := os.Symlink(link, target); err != nil {
			return fmt.Errorf("create link %s: %w", target, err)
		}
		if err := os.Lchown(target, int(st.Uid), int(st.Gid)); err != nil {
			return fmt.Errorf("chown %s: %w", target, err)
		}
		return nil
	case mode.IsRegular():
		key := [2]uint64{st.Dev, st.Ino}
		if first, seen := hardlinks[key]; seen {
			if err := os.Link(first, target); err != nil {
				return fmt.Errorf("link %s: %w", target, err)
			}
			return nil
		}
		if st.Nlink > 1 {
			hardlinks[key] = target
		}
		if err := copyFile(path, target); err != nil {
			return err
		}
	case mode&os.ModeSocket != 0:
		return nil
	default:
		// Devices and FIFOs.
		if err := unix.Mknod(target, st.Mode, int(st.Rdev)); err != nil { //nolint:gosec // device numbers fit
			return fmt.Errorf("create %s: %w", target, err)
		}
	}
	// Ownership first: chown clears set-id bits.
	if err := os.Lchown(target, int(st.Uid), int(st.Gid)); err != nil {
		return fmt.Errorf("chown %s: %w", target, err)
	}
	if err := os.Chmod(target, fileMode(int64(st.Mode&0o7777))); err != nil {
		return fmt.Errorf("chmod %s: %w", target, err)
	}
	attrs, err := extendedAttributes(path)
	if err != nil {
		return err
	}
	for name, value := range attrs {
		name = strings.TrimPrefix(name, "SCHILY.xattr.")
		if err := unix.Lsetxattr(target, name, []byte(value), 0); err != nil && !errors.Is(err, unix.ENOTSUP) {
			return fmt.Errorf("set attribute %s of %s: %w", name, target, err)
		}
	}
	if !mode.IsDir() {
		_ = os.Chtimes(target, info.ModTime(), info.ModTime())
	}
	return nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src) //nolint:gosec // seeding copies the whole image
	if err != nil {
		return fmt.Errorf("open %s: %w", src, err)
	}
	defer func() { _ = in.Close() }()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600) //nolint:gosec // see above
	if err != nil {
		return fmt.Errorf("create %s: %w", dst, err)
	}
	if _, err := io.Copy(out, in); err != nil {
		_ = out.Close()
		return fmt.Errorf("copy %s: %w", src, err)
	}
	if err := out.Close(); err != nil {
		return fmt.Errorf("copy %s: %w", src, err)
	}
	return nil
}
