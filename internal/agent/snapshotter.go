package agent

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"slices"
	"time"

	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/diskengine"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

// snapshotterWait is how often an agent whose host still runs the previous
// release's snapshotter looks for the host to run nothing.
const snapshotterWait = 15 * time.Second

// ErrSnapshotterReplaceable ends Run once a host that kept the previous
// release's snapshotter runs nothing; the service restarts the agent, which
// replaces it.
var ErrSnapshotterReplaceable = errors.New("the host runs nothing; the snapshotter can be replaced")

// errPreviousSnapshotter refuses a start that mounts platform volumes on a
// host that keeps the previous release's snapshotter, which cannot serve
// this release's mounts.
var errPreviousSnapshotter = errors.New("the host keeps the previous release's snapshotter until it runs nothing")

// replaceSnapshotter installs the release's snapshotter in place of the one
// the host runs when nothing uses it, and reports whether the host keeps
// another because something does. Restarting it ends every FUSE mount it
// serves.
func (a *Agent) replaceSnapshotter(ctx context.Context) (bool, error) {
	idle, err := a.idle(ctx)
	if err != nil || !idle {
		previous, replacedErr := layersource.Replaced(a.cfg.Executable)
		return previous != nil, errors.Join(err, replacedErr)
	}
	if changed, err := layersource.Install(a.cfg.Executable); err != nil || !changed {
		return false, err //nolint:wrapcheck // It names the step.
	}
	// The unit is Type=notify, so the restart returns once it serves.
	if out, err := exec.CommandContext(ctx, "systemctl", "restart", layersource.Unit).CombinedOutput(); err != nil { //nolint:gosec // A fixed unit name.
		return false, fmt.Errorf("restart the snapshotter: %w: %s", err, out)
	}
	a.log.Info("replaced the snapshotter with the release's")
	return false, nil
}

// idle reports whether nothing uses the snapshotter: no container of the
// host runs, mount containers still storing writes included, and no disk is
// attached.
func (a *Agent) idle(ctx context.Context) (bool, error) {
	list, err := a.docker.ContainerList(ctx, client.ContainerListOptions{Filters: client.Filters{}.Add("label", labelHost+"="+a.identity.HostID)})
	if err != nil {
		return false, fmt.Errorf("list running containers: %w", err)
	}
	attached := false
	if a.diskErr == nil {
		disks, err := a.diskEngine.List(ctx)
		if err != nil {
			return false, fmt.Errorf("list local disks: %w", err)
		}
		attached = slices.ContainsFunc(disks, func(d diskengine.LocalDisk) bool { return d.Attached })
	}
	return len(list.Items) == 0 && !attached, nil
}

// awaitIdle restarts the agent once its host, which kept the previous
// release's snapshotter, runs nothing.
func (a *Agent) awaitIdle(ctx context.Context) {
	tick := time.NewTicker(snapshotterWait)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		if idle, err := a.idle(ctx); err != nil {
			a.log.Warn("checking whether the host runs anything failed", "error", err)
		} else if idle {
			a.restart(ErrSnapshotterReplaceable)
			return
		}
	}
}

// hostVersion is the release the host runs: the agent's, or none while it
// keeps the previous release's snapshotter, so the server holds new work
// off the host as off one whose agent must update.
func (a *Agent) hostVersion() string {
	if a.previousSnapshotter {
		return ""
	}
	return a.cfg.Version
}
