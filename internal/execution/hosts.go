package execution

import (
	"context"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
)

var _ compute.Containers = (*Execution)(nil)

// StopHostContainers stops every live container on host in tx because the
// host stops serving: removed, reclaimed or gone. Running attempts are lost
// and retried by policy. It locks the containers in id order before
// finishing any attempt, keeping the container, task, attempt lock order.
func (e *Execution) StopHostContainers(ctx context.Context, tx pgx.Tx, host compute.HostID, message string) (int, error) {
	id := uuid.UUID(host)
	containers, err := e.queries.WithTx(tx).LockLiveContainersOnHost(ctx, &id)
	if err != nil {
		return 0, fmt.Errorf("lock live containers: %w", err)
	}
	for _, container := range containers {
		if err := e.containerExited(ctx, tx, ContainerID(container), ContainerExit{Reason: StopHostLost, Message: message}); err != nil {
			return 0, fmt.Errorf("stop container %s: %w", container, err)
		}
	}
	return len(containers), nil
}

// DrainHostContainers moves the host's starting and ready containers to
// draining in tx: they claim nothing more and the host stops each once its
// running attempts finish. Planning replaces them elsewhere as demand needs.
func (e *Execution) DrainHostContainers(ctx context.Context, tx pgx.Tx, host compute.HostID) error {
	drained, err := e.queries.WithTx(tx).DrainContainersOnHost(ctx, ptr(uuid.UUID(host)))
	if err != nil {
		return fmt.Errorf("drain containers: %w", err)
	}
	if len(drained) == 0 {
		return nil
	}
	if err := database.Notify(ctx, tx, database.ChannelHost, host.String()); err != nil {
		return err
	}
	for _, c := range drained {
		channel, id := database.ChannelExecution, ""
		switch {
		case c.ReleaseID != nil:
			id = c.ReleaseID.String()
		case c.ImageBuildID != nil:
			channel, id = database.ChannelImageBuild, c.ImageBuildID.String()
		}
		if err := database.Notify(ctx, tx, channel, id); err != nil {
			return err
		}
	}
	return nil
}

// StoppedAmong returns the containers of ids that are stopped. A host
// session asks about the containers its host still runs, so a stop decided
// elsewhere reaches the host.
func (e *Execution) StoppedAmong(ctx context.Context, ids []ContainerID) ([]ContainerID, error) {
	if len(ids) == 0 {
		return nil, nil
	}
	raw := make([]uuid.UUID, len(ids))
	for n, id := range ids {
		raw[n] = uuid.UUID(id)
	}
	rows, err := e.queries.StoppedContainersAmong(ctx, raw)
	if err != nil {
		return nil, fmt.Errorf("read stopped containers: %w", err)
	}
	out := make([]ContainerID, len(rows))
	for n, row := range rows {
		out[n] = ContainerID(row)
	}
	return out, nil
}
