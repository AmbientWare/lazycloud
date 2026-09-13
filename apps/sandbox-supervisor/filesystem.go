package main

import (
	"archive/tar"
	"bufio"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
)

func snapshotFilesystem(target string) error {
	mountInfo, err := os.Open("/proc/self/mountinfo")
	if err != nil {
		return err
	}
	defer mountInfo.Close()
	mounts := make(map[string]bool)
	scanner := bufio.NewScanner(mountInfo)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 6 {
			return fmt.Errorf("invalid filesystem mount record")
		}
		mount, err := strconv.Unquote(`"` + fields[4] + `"`)
		if err != nil {
			return err
		}
		if mount != "/" {
			mounts[mount] = true
		}
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	file, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	defer file.Close()
	archive := tar.NewWriter(file)
	hardlinks := make(map[[2]uint64]string)
	err = filepath.WalkDir("/", func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if path == "/" {
			return nil
		}
		if mounts[path] {
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSocket != 0 {
			return nil
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
		header.Name = strings.TrimPrefix(path, "/")
		if info.Mode()&os.ModeSymlink == 0 {
			attributes, err := filesystemAttributes(path)
			if err != nil {
				return err
			}
			header.PAXRecords = attributes
		}
		if info.Mode().IsRegular() {
			stat := info.Sys().(*syscall.Stat_t)
			key := [2]uint64{uint64(stat.Dev), stat.Ino}
			if first, exists := hardlinks[key]; exists {
				header.Typeflag = tar.TypeLink
				header.Linkname = first
				header.Size = 0
			} else if stat.Nlink > 1 {
				hardlinks[key] = header.Name
			}
		}
		if err := archive.WriteHeader(header); err != nil {
			return err
		}
		if header.Typeflag != tar.TypeReg {
			return nil
		}
		input, err := os.Open(path)
		if err != nil {
			return err
		}
		_, copyErr := io.CopyN(archive, input, header.Size)
		closeErr := input.Close()
		if copyErr != nil {
			return copyErr
		}
		return closeErr
	})
	if err != nil {
		return err
	}
	if err := archive.Close(); err != nil {
		return err
	}
	return file.Sync()
}

func filesystemAttributes(path string) (map[string]string, error) {
	size, err := syscall.Listxattr(path, nil)
	if errors.Is(err, syscall.ENOTSUP) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	names := make([]byte, size)
	if _, err := syscall.Listxattr(path, names); err != nil {
		return nil, err
	}
	attributes := make(map[string]string)
	for _, name := range strings.Split(string(names), "\x00") {
		if name == "" {
			continue
		}
		size, err := syscall.Getxattr(path, name, nil)
		if err != nil {
			return nil, err
		}
		value := make([]byte, size)
		if _, err := syscall.Getxattr(path, name, value); err != nil {
			return nil, err
		}
		attributes["SCHILY.xattr."+name] = string(value)
	}
	return attributes, nil
}
