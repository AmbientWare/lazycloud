package agent

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"github.com/moby/moby/client"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

// replaceSnapshotter installs the release's snapshotter in place of the one
// the host runs when they differ and nothing uses it: no container of the
// host runs, mount containers still storing writes included, and no disk is
// attached. Restarting it ends every FUSE mount it serves.
func (a *Agent) replaceSnapshotter(ctx context.Context) error {
	exe, err := filepath.EvalSymlinks(a.cfg.Executable)
	if err != nil {
		return nil //nolint:nilerr // An agent outside a release brings no snapshotter.
	}
	release, err := os.ReadFile(filepath.Join(filepath.Dir(exe), SnapshotterExecutable)) //nolint:gosec // The agent's own release.
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read the release's snapshotter: %w", err)
	}
	if running, err := os.ReadFile(layersource.Binary); err == nil && bytes.Equal(running, release) {
		return nil
	}
	list, err := a.docker.ContainerList(ctx, client.ContainerListOptions{Filters: client.Filters{}.Add("label", labelHost+"="+a.identity.HostID)})
	if err != nil {
		return fmt.Errorf("list running containers: %w", err)
	}
	attached := false
	if a.diskErr == nil {
		disks, err := a.diskEngine.List(ctx)
		if err != nil {
			return fmt.Errorf("list local disks: %w", err)
		}
		for _, d := range disks {
			attached = attached || d.Attached
		}
	}
	if len(list.Items) > 0 || attached {
		a.log.Info("the host keeps its snapshotter while containers or disks use it")
		return nil
	}
	if err := writeFileAtomic(layersource.Binary, release, 0o755); err != nil { //nolint:gosec // An executable.
		return fmt.Errorf("install the snapshotter: %w", err)
	}
	conn, err := a.volumes.systemd(ctx)
	if err != nil {
		return err
	}
	done := make(chan string, 1)
	if _, err := conn.RestartUnitContext(ctx, layersource.Unit, "replace", done); err != nil {
		return fmt.Errorf("restart the snapshotter: %w", err)
	}
	select {
	case result := <-done:
		if result != "done" {
			return fmt.Errorf("restart the snapshotter: %s", result)
		}
	case <-ctx.Done():
		return fmt.Errorf("restart the snapshotter: %w", ctx.Err())
	}
	a.log.Info("replaced the snapshotter with the release's")
	return nil
}

// cachesVolumes reports whether the host mounts platform volumes through
// its snapshotter's endpoint: a GPU host, whose data volume is instance
// store, with a snapshotter that serves VolumeSources.
func (a *Agent) cachesVolumes(ctx context.Context, snapshotter *layersource.Client) (bool, error) {
	if len(a.gpus) == 0 && !a.cfg.cacheVolumes {
		return false, nil
	}
	ctx, cancel := context.WithTimeout(ctx, grantTimeout)
	defer cancel()
	_, err := snapshotter.Volumes.WarmVolumes(ctx, &imagefsproto.WarmVolumesRequest{})
	if status.Code(err) == codes.Unimplemented {
		a.log.Info("the host's snapshotter serves no volumes; volumes mount from their buckets")
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("ask the snapshotter for volumes: %w", err)
	}
	return true, nil
}
