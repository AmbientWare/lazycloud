package agent

import (
	"archive/tar"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// removedRecord marks a tar entry whose path the sync removes.
const removedRecord = "LAZYCLOUD.removed"

// serveSync applies a tar of source changes to a container's workspace, then
// asks its supervisor to restart the runners once their work finishes.
func (d *dataLink) serveSync(stream forwardStream, in *inbound, c *container) {
	if c == nil || c.hasExited() {
		_ = stream.Send(forwardError(hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_NOT_RUNNING, "the container does not run on this host"))
		return
	}
	written, removed, err := applySync(c.workspaceDir(), in)
	if err != nil {
		_ = stream.Send(forwardError(hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_FAILED, err.Error()))
		return
	}
	c.reload()
	c.log.Info("workspace synced", "written", written, "removed", removed)
	if err := stream.Send(&hostproto.ForwardUp{Body: &hostproto.ForwardUp_Synced{Synced: &hostproto.SyncResult{
		Written: int32(written), Removed: int32(removed), //nolint:gosec // bounded by maxWorkspaceFiles
	}}}); err != nil {
		return
	}
	_ = stream.Send(&hostproto.ForwardUp{Body: &hostproto.ForwardUp_End{End: &hostproto.End{}}})
}

// applySync writes and removes files under dir. Paths cannot leave it, even
// through symbolic links already in the workspace, and each file is
// replaced whole, so a runner never imports half of one.
func applySync(dir string, r io.Reader) (written, removed int, err error) {
	root, err := os.OpenRoot(dir)
	if err != nil {
		return 0, 0, fmt.Errorf("open workspace: %w", err)
	}
	defer func() { _ = root.Close() }()
	archive := tar.NewReader(r)
	for {
		header, err := archive.Next()
		if errors.Is(err, io.EOF) {
			return written, removed, nil
		}
		if err != nil {
			return written, removed, fmt.Errorf("read sync archive: %w", err)
		}
		if written+removed >= maxWorkspaceFiles {
			return written, removed, fmt.Errorf("a sync changes at most %d files", maxWorkspaceFiles)
		}
		name := filepath.FromSlash(header.Name)
		if !filepath.IsLocal(name) {
			return written, removed, fmt.Errorf("sync entry %q leaves the workspace", header.Name)
		}
		if header.PAXRecords[removedRecord] == "1" {
			if err := root.Remove(name); err != nil && !errors.Is(err, fs.ErrNotExist) {
				return written, removed, fmt.Errorf("remove %s: %w", header.Name, err)
			}
			removed++
			continue
		}
		switch header.Typeflag {
		case tar.TypeDir:
			if err := root.MkdirAll(name, 0o755); err != nil {
				return written, removed, fmt.Errorf("create %s: %w", header.Name, err)
			}
			continue
		case tar.TypeReg:
		default:
			return written, removed, fmt.Errorf("sync entry %q is not a regular file", header.Name)
		}
		if err := writeSynced(root, name, header.FileInfo().Mode().Perm(), archive); err != nil {
			return written, removed, fmt.Errorf("write %s: %w", header.Name, err)
		}
		written++
	}
}

func writeSynced(root *os.Root, name string, perm fs.FileMode, content io.Reader) error {
	if parent := filepath.Dir(name); parent != "." {
		if err := root.MkdirAll(parent, 0o755); err != nil {
			return fmt.Errorf("create directory: %w", err)
		}
	}
	temp := filepath.Join(filepath.Dir(name), "."+filepath.Base(name)+".lazycloud-sync")
	file, err := root.OpenFile(temp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, perm|0o600)
	if err != nil {
		return fmt.Errorf("create: %w", err)
	}
	if _, err := io.Copy(file, content); err != nil {
		_ = file.Close()
		_ = root.Remove(temp)
		return fmt.Errorf("copy: %w", err)
	}
	if err := file.Close(); err != nil {
		_ = root.Remove(temp)
		return fmt.Errorf("close: %w", err)
	}
	if err := root.Rename(temp, name); err != nil {
		_ = root.Remove(temp)
		return fmt.Errorf("replace: %w", err)
	}
	return nil
}
