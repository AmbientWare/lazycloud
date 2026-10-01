package agent

import (
	"context"
	"fmt"

	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// detectDiskQuota reports whether Docker can limit a container's writable
// layer: overlay2 enforces --storage-opt size only on XFS with project
// quotas. Other hosts run without the limit, and say so once at startup.
func detectDiskQuota(ctx context.Context, docker *client.Client) (bool, error) {
	info, err := docker.Info(ctx, client.InfoOptions{})
	if err != nil {
		return false, fmt.Errorf("read docker info: %w", err)
	}
	if info.Info.Driver != "overlay2" {
		return false, nil
	}
	for _, kv := range info.Info.DriverStatus {
		if kv[0] == "Backing Filesystem" {
			return kv[1] == "xfs", nil
		}
	}
	return false, nil
}

// diskLimit is the writable layer limit for a container's resources.
func (a *Agent) diskLimit(r *hostproto.Resources) map[string]string {
	if !a.diskQuota || r.GetDiskLimitBytes() <= 0 {
		return nil
	}
	return map[string]string{"size": fmt.Sprintf("%d", r.GetDiskLimitBytes())}
}
