package main

import (
	"archive/tar"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

const snapshotChunkBytes = 64 * 1024

type snapshotWriter struct {
	connection net.Conn
	encoder    *json.Encoder
}

func (w snapshotWriter) Write(data []byte) (int, error) {
	written := 0
	for len(data) > 0 {
		size := min(len(data), snapshotChunkBytes)
		if err := w.connection.SetWriteDeadline(time.Now().Add(30 * time.Second)); err != nil {
			return written, err
		}
		if err := w.encoder.Encode(response{Version: protocolVersion, Type: "snapshot-chunk", Data: data[:size]}); err != nil {
			return written, err
		}
		written += size
		data = data[size:]
	}
	return written, nil
}

func (s *supervisor) handleFilesystemSnapshot(connection net.Conn, encoder *json.Encoder, excludes []string) {
	excluded := map[string]bool{"/proc": true, "/dev": true, "/sys": true}
	for _, path := range excludes {
		if !filepath.IsAbs(path) || filepath.Clean(path) != path || path == "/" {
			writeError(encoder, errors.New("invalid filesystem snapshot exclusion"))
			return
		}
		excluded[path] = true
	}
	archive := tar.NewWriter(snapshotWriter{connection: connection, encoder: encoder})
	hardlinks := make(map[[2]uint64]string)
	err := filepath.WalkDir("/", func(path string, entry fs.DirEntry, walkError error) error {
		if walkError != nil {
			return walkError
		}
		if excluded[path] {
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeNamedPipe != 0 {
			return fmt.Errorf("filesystem snapshot cannot include FIFO %s: restored FIFOs cannot be opened under sandbox isolation", path)
		}
		if !info.Mode().IsRegular() && !info.IsDir() && info.Mode()&os.ModeSymlink == 0 {
			return fmt.Errorf("unsupported filesystem snapshot file type at %s: %s", path, info.Mode().Type())
		}
		link := ""
		if info.Mode()&os.ModeSymlink != 0 {
			link, err = os.Readlink(path)
			if err != nil {
				return err
			}
		}
		header, err := tar.FileInfoHeader(info, link)
		if err != nil {
			return err
		}
		header.Xattrs, err = snapshotAttributes(path, info)
		if err != nil {
			return fmt.Errorf("filesystem snapshot metadata at %s: %w", path, err)
		}
		header.Format = tar.FormatPAX
		header.Name = strings.TrimPrefix(path, "/")
		if header.Name == "" {
			header.Name = "."
		}
		if info.Mode().IsRegular() {
			metadata, ok := info.Sys().(*syscall.Stat_t)
			if !ok {
				return errors.New("filesystem snapshot is missing Unix metadata")
			}
			identity := [2]uint64{uint64(metadata.Dev), metadata.Ino}
			if existing, found := hardlinks[identity]; found {
				header.Typeflag, header.Linkname, header.Size = tar.TypeLink, existing, 0
			} else if metadata.Nlink > 1 {
				hardlinks[identity] = header.Name
			}
		}
		if err := archive.WriteHeader(header); err != nil {
			return err
		}
		if header.Typeflag != tar.TypeReg {
			return nil
		}
		file, err := openSnapshotFile(path)
		if err != nil {
			return err
		}
		defer file.Close()
		opened, err := file.Stat()
		if err != nil {
			return err
		}
		if !os.SameFile(info, opened) {
			return errors.New("file replaced during filesystem snapshot: " + path)
		}
		if _, err := io.CopyN(archive, file, header.Size); err != nil {
			return err
		}
		after, err := file.Stat()
		if err != nil {
			return err
		}
		if !os.SameFile(info, after) || after.Size() != info.Size() || !after.ModTime().Equal(info.ModTime()) {
			return errors.New("file changed during filesystem snapshot: " + path)
		}
		return nil
	})
	if err == nil {
		err = archive.Close()
	}
	if err != nil {
		writeError(encoder, err)
		return
	}
	_ = encoder.Encode(response{Version: protocolVersion, Type: "snapshot-complete"})
}

func openSnapshotFile(path string) (*os.File, error) {
	fd, err := syscall.Open("/", syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	if path == "/" {
		return os.NewFile(uintptr(fd), path), nil
	}
	parts := strings.Split(strings.TrimPrefix(path, "/"), "/")
	for index, part := range parts {
		flags := syscall.O_RDONLY | syscall.O_NOFOLLOW | syscall.O_CLOEXEC | syscall.O_NONBLOCK
		if index < len(parts)-1 {
			flags |= syscall.O_DIRECTORY
		}
		next, openErr := syscall.Openat(fd, part, flags, 0)
		_ = syscall.Close(fd)
		if openErr != nil {
			return nil, openErr
		}
		fd = next
	}
	return os.NewFile(uintptr(fd), path), nil
}

func snapshotAttributes(path string, info fs.FileInfo) (map[string]string, error) {
	metadataPath := path
	var file *os.File
	var err error
	if info.Mode()&os.ModeSymlink != 0 {
		file, err = openSnapshotFile(filepath.Dir(path))
		if err == nil {
			metadataPath = fmt.Sprintf("/proc/self/fd/%d/%s", file.Fd(), filepath.Base(path))
		}
	} else {
		file, err = openSnapshotFile(path)
		if err == nil {
			metadataPath = fmt.Sprintf("/proc/self/fd/%d", file.Fd())
		}
	}
	if err != nil {
		return nil, err
	}
	defer file.Close()
	list := unix.Listxattr
	get := unix.Getxattr
	stat := os.Stat
	if info.Mode()&os.ModeSymlink != 0 {
		list, get, stat = unix.Llistxattr, unix.Lgetxattr, os.Lstat
	}
	current, err := stat(metadataPath)
	if err != nil || !os.SameFile(info, current) {
		return nil, errors.New("file replaced while reading attributes")
	}
	size, err := list(metadataPath, nil)
	if errors.Is(err, unix.ENOTSUP) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	names := make([]byte, size)
	size, err = list(metadataPath, names)
	if err != nil {
		return nil, err
	}
	attributes := make(map[string]string)
	for _, name := range strings.Split(string(names[:size]), "\x00") {
		if name == "" {
			continue
		}
		size, err := get(metadataPath, name, nil)
		if err != nil {
			return nil, err
		}
		value := make([]byte, size)
		size, err = get(metadataPath, name, value)
		if err != nil {
			return nil, err
		}
		attributes[name] = string(value[:size])
	}
	current, err = stat(metadataPath)
	if err != nil || !os.SameFile(info, current) {
		return nil, errors.New("file replaced while reading attributes")
	}
	return attributes, nil
}
