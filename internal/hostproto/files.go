package hostproto

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// PlaceFile writes r to a temporary file beside path, syncs it, places it
// at path with place and syncs the directory. os.Rename replaces path;
// os.Link keeps a file already there and fails with fs.ErrExist.
func PlaceFile(path string, r io.Reader, mode os.FileMode, place func(tmp, path string) error) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".*.tmp")
	if err != nil {
		return fmt.Errorf("create temporary file: %w", err)
	}
	defer func() { _ = os.Remove(tmp.Name()) }()
	err = tmp.Chmod(mode)
	if err == nil {
		_, err = io.Copy(tmp, r)
	}
	if err == nil {
		err = tmp.Sync()
	}
	if err = errors.Join(err, tmp.Close()); err != nil {
		return fmt.Errorf("write %s: %w", path, err)
	}
	if err := place(tmp.Name(), path); err != nil {
		return fmt.Errorf("place %s: %w", path, err)
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return fmt.Errorf("open %s: %w", filepath.Dir(path), err)
	}
	if err := errors.Join(dir.Sync(), dir.Close()); err != nil {
		return fmt.Errorf("sync %s: %w", filepath.Dir(path), err)
	}
	return nil
}

// WriteFile replaces path with data as PlaceFile does.
func WriteFile(path string, data []byte, mode os.FileMode) error {
	return PlaceFile(path, bytes.NewReader(data), mode, os.Rename)
}
