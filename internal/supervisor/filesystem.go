package supervisor

import (
	"archive/tar"
	"bufio"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"
)

// mountPoint is one line of /proc/self/mountinfo.
type mountPoint struct {
	path string
	// options are the per-mount options, such as ro and nosuid.
	options []string
}

// readMounts lists the mount points of the supervisor's mount namespace
// below its root, in mount order.
func readMounts() ([]mountPoint, error) {
	file, err := os.Open("/proc/self/mountinfo")
	if err != nil {
		return nil, fmt.Errorf("read mounts: %w", err)
	}
	defer func() { _ = file.Close() }()
	var mounts []mountPoint
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 6 {
			return nil, errors.New("invalid mountinfo record")
		}
		// Spaces and other bytes are octal escapes, as in Go strings.
		path, err := strconv.Unquote(`"` + fields[4] + `"`)
		if err != nil {
			return nil, fmt.Errorf("decode mount point %q: %w", fields[4], err)
		}
		mounts = append(mounts, mountPoint{path: path, options: strings.Split(fields[5], ",")})
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("read mounts: %w", err)
	}
	return mounts, nil
}

// mountSet is the mount points other than /, which an archive or a seed of
// the root filesystem leaves out.
func mountSet(mounts []mountPoint) map[string]bool {
	set := make(map[string]bool, len(mounts))
	for _, m := range mounts {
		if m.path != "/" {
			set[m.path] = true
		}
	}
	return set
}

// archiveFilesystem streams a tar of / without its mounts as it walks.
func (c *control) archiveFilesystem(w http.ResponseWriter, _ *http.Request) error {
	mounts, err := readMounts()
	if err != nil {
		return err
	}
	skip := mountSet(mounts)
	// gVisor mounts a tmpfs at /tmp when the image's is empty; what the
	// workload wrote there is its own, as under runc.
	delete(skip, "/tmp")
	w.Header().Set("Content-Type", "application/x-tar")
	w.WriteHeader(http.StatusOK)
	if err := writeArchive(w, "/", skip); err != nil {
		c.log.Error("filesystem archive failed", "error", err)
		// The status is sent; an aborted body keeps a partial archive from
		// looking complete.
		panic(http.ErrAbortHandler)
	}
	return nil
}

// writeArchive writes root's tree to w with paths relative to root, leaving
// out the paths in skip and sockets, and keeping hardlinks and extended
// attributes. Entries that vanish during the walk are left out.
func writeArchive(w io.Writer, root string, skip map[string]bool) error {
	archive := tar.NewWriter(w)
	hardlinks := make(map[[2]uint64]string)
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			// A directory the container's user cannot read is left out,
			// as a non-root container's /root is.
			if errors.Is(walkErr, fs.ErrNotExist) || errors.Is(walkErr, fs.ErrPermission) {
				return nil
			}
			return walkErr
		}
		if path == root {
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
		if info.Mode()&os.ModeSocket != 0 {
			return nil
		}
		return archiveEntry(archive, root, path, info, hardlinks)
	})
	if err != nil {
		return fmt.Errorf("walk %s: %w", root, err)
	}
	if err := archive.Close(); err != nil {
		return fmt.Errorf("finish archive: %w", err)
	}
	return nil
}

func archiveEntry(archive *tar.Writer, root, path string, info fs.FileInfo, hardlinks map[[2]uint64]string) error {
	link := ""
	if info.Mode()&os.ModeSymlink != 0 {
		var err error
		if link, err = os.Readlink(path); err != nil {
			return fmt.Errorf("read link %s: %w", path, err)
		}
	}
	header, err := tar.FileInfoHeader(info, link)
	if err != nil {
		return fmt.Errorf("archive header for %s: %w", path, err)
	}
	rel, err := filepath.Rel(root, path)
	if err != nil {
		return fmt.Errorf("archive path for %s: %w", path, err)
	}
	header.Name = filepath.ToSlash(rel)
	if info.IsDir() {
		header.Name += "/"
	}
	// Numeric owners only: the image's names may differ from the
	// supervisor's view of them.
	header.Uname, header.Gname = "", ""
	if info.Mode()&os.ModeSymlink == 0 {
		if header.PAXRecords, err = extendedAttributes(path); err != nil {
			return err
		}
	}
	if st, ok := info.Sys().(*syscall.Stat_t); ok && info.Mode().IsRegular() {
		key := [2]uint64{st.Dev, st.Ino}
		if first, seen := hardlinks[key]; seen {
			header.Typeflag, header.Linkname, header.Size = tar.TypeLink, first, 0
		} else if st.Nlink > 1 {
			hardlinks[key] = header.Name
		}
	}
	var file *os.File
	if header.Typeflag == tar.TypeReg {
		// A file the container's user cannot read, such as /etc/shadow for
		// a non-root user, is left out rather than failing the image.
		file, err = os.Open(path) //nolint:gosec // the archive reads the whole filesystem
		if errors.Is(err, fs.ErrPermission) || errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("open %s: %w", path, err)
		}
		defer func() { _ = file.Close() }()
	}
	if err := archive.WriteHeader(header); err != nil {
		return fmt.Errorf("write archive header for %s: %w", path, err)
	}
	if file == nil {
		return nil
	}
	// The header holds the size at stat; a file that grew is cut there and
	// one that shrank fails the archive.
	if _, err := io.CopyN(archive, file, header.Size); err != nil {
		return fmt.Errorf("archive %s: %w", path, err)
	}
	return nil
}

func extendedAttributes(path string) (map[string]string, error) {
	size, err := unix.Llistxattr(path, nil)
	if errors.Is(err, unix.ENOTSUP) || size == 0 {
		return nil, nil //nolint:nilnil // no attributes
	}
	if err != nil {
		return nil, fmt.Errorf("list attributes of %s: %w", path, err)
	}
	names := make([]byte, size)
	if size, err = unix.Llistxattr(path, names); err != nil {
		return nil, fmt.Errorf("list attributes of %s: %w", path, err)
	}
	records := make(map[string]string)
	for name := range strings.SplitSeq(string(names[:size]), "\x00") {
		if name == "" {
			continue
		}
		n, err := unix.Lgetxattr(path, name, nil)
		if err != nil {
			return nil, fmt.Errorf("read attribute %s of %s: %w", name, path, err)
		}
		value := make([]byte, n)
		if n, err = unix.Lgetxattr(path, name, value); err != nil {
			return nil, fmt.Errorf("read attribute %s of %s: %w", name, path, err)
		}
		records["SCHILY.xattr."+name] = string(value[:n])
	}
	return records, nil
}
