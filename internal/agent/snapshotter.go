package agent

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
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
	// The unit is Type=notify, so the restart returns once it serves.
	if out, err := exec.CommandContext(ctx, "systemctl", "restart", layersource.Unit).CombinedOutput(); err != nil { //nolint:gosec // A fixed unit name.
		return fmt.Errorf("restart the snapshotter: %w: %s", err, out)
	}
	a.log.Info("replaced the snapshotter with the release's")
	return nil
}

// volumeEndpoint is the address of the snapshotter's endpoint for platform
// volumes, empty when it does not cache them.
func (a *Agent) volumeEndpoint(ctx context.Context) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, grantTimeout)
	defer cancel()
	answer, err := a.volumes.sources.GrantVolumes(ctx, &imagefsproto.GrantVolumesRequest{})
	if status.Code(err) == codes.Unimplemented {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("ask the snapshotter for its volume endpoint: %w", err)
	}
	return answer.GetEndpoint(), nil
}
