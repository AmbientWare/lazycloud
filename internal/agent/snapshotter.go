package agent

import (
	"context"
	"fmt"
	"os/exec"

	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

// replaceSnapshotter installs the release's snapshotter in place of the one
// the host runs when nothing uses it: no container of the host runs, mount
// containers still storing writes included, and no disk is attached.
// Restarting it ends every FUSE mount it serves.
func (a *Agent) replaceSnapshotter(ctx context.Context) error {
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
		return nil
	}
	if changed, err := layersource.Install(a.cfg.Executable); err != nil || !changed {
		return err //nolint:wrapcheck // It names the step.
	}
	// The unit is Type=notify, so the restart returns once it serves.
	if out, err := exec.CommandContext(ctx, "systemctl", "restart", layersource.Unit).CombinedOutput(); err != nil { //nolint:gosec // A fixed unit name.
		return fmt.Errorf("restart the snapshotter: %w: %s", err, out)
	}
	a.log.Info("replaced the snapshotter with the release's")
	return nil
}
