package execution

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// Memory snapshots checkpoint a running container through its host and are
// stored by id; filesystem images publish a container's filesystem. Both
// are requested here, delivered to the host as derived commands and
// finished by the host's report.

const (
	// SnapshotDeadline bounds how long a host may take to store a snapshot.
	SnapshotDeadline = 45 * time.Minute
	// PublishDeadline bounds how long a host may take to publish a
	// filesystem image.
	PublishDeadline = time.Hour
)

// Snapshot is a memory snapshot.
type Snapshot struct {
	ID        uuid.UUID
	Container *uuid.UUID
	Release   uuid.UUID
	Workspace identity.WorkspaceID
	Automatic bool
	State     apitypes.MemorySnapshotState
	Failure   *string
	SizeBytes *int64
	SHA256    *string
	CreatedAt time.Time
}

// UnsupportedError means a host cannot do what was asked, such as a runc
// host without CRIU checkpointing.
type UnsupportedError struct{ Reason string }

func (e *UnsupportedError) Error() string { return e.Reason }

// CreateSnapshot records a pending snapshot of a ready container and wakes
// its host, which takes it.
func (e *Execution) CreateSnapshot(ctx context.Context, workspace identity.WorkspaceID, container ContainerID, id *uuid.UUID) (Snapshot, error) {
	var snapshot uuid.UUID
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		q := e.queries.WithTx(tx)
		row, err := q.LockSnapshotSource(ctx, LockSnapshotSourceParams{ID: uuid.UUID(container), WorkspaceID: uuid.UUID(workspace)})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("lock container: %w", err)
		}
		if ContainerState(row.State) != ContainerReady || row.HostID == nil {
			return &ConflictError{Reason: "only a running container can be snapshotted"}
		}
		inserted, err := q.InsertSnapshot(ctx, InsertSnapshotParams{
			ID: id, WorkspaceID: uuid.UUID(workspace), ReleaseID: row.ReleaseID, ContainerID: &row.ID,
		})
		if isUniqueViolation(err) {
			return &ConflictError{Reason: "a snapshot with that id exists"}
		}
		if err != nil {
			return fmt.Errorf("insert snapshot: %w", err)
		}
		snapshot = inserted.ID
		return database.Notify(ctx, tx, database.ChannelHost, row.HostID.String())
	})
	if err != nil {
		return Snapshot{}, fmt.Errorf("snapshot container %s: %w", container, err)
	}
	return e.Snapshot(ctx, snapshot)
}

// Snapshot reads a snapshot by id.
func (e *Execution) Snapshot(ctx context.Context, id uuid.UUID) (Snapshot, error) {
	row, err := e.queries.SnapshotView(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return Snapshot{}, ErrNotFound
	}
	if err != nil {
		return Snapshot{}, fmt.Errorf("read snapshot: %w", err)
	}
	return Snapshot{
		ID: row.ID, Container: row.ContainerID, Release: row.ReleaseID, Workspace: identity.WorkspaceID(row.WorkspaceID),
		Automatic: row.Automatic, State: apitypes.MemorySnapshotState(row.State), Failure: row.Failure,
		SizeBytes: row.SizeBytes, SHA256: row.Sha256, CreatedAt: row.CreatedAt,
	}, nil
}

// WaitSnapshot waits until the snapshot is no longer pending or ctx ends.
func (e *Execution) WaitSnapshot(ctx context.Context, listener *database.Listener, id uuid.UUID) (Snapshot, error) {
	wake, cancel := listener.Subscribe(database.ChannelContainerOp, id.String())
	defer cancel()
	poll := time.NewTicker(5 * time.Second)
	defer poll.Stop()
	for {
		s, err := e.Snapshot(ctx, id)
		if err != nil || s.State != apitypes.MemorySnapshotStatePending {
			return s, err
		}
		select {
		case <-ctx.Done():
			return s, fmt.Errorf("wait for snapshot: %w", ctx.Err())
		case <-wake:
		case <-poll.C:
		}
	}
}

// SnapshotOutcome is a host's report on a snapshot.
type SnapshotOutcome struct {
	Snapshot    uuid.UUID
	Container   ContainerID
	SizeBytes   int64
	SHA256      string
	Failure     string
	Unsupported bool
}

// ErrStaleSnapshot means the report is for a snapshot that already ended or
// a container the host does not hold.
var ErrStaleSnapshot = errors.New("the snapshot is not pending on this host")

// CompleteSnapshot records the host's outcome once.
func (e *Execution) CompleteSnapshot(ctx context.Context, host compute.HostID, out SnapshotOutcome) error {
	params := FinishSnapshotParams{ID: out.Snapshot, ContainerID: uuid.UUID(out.Container), HostID: hostUUID(host)}
	switch {
	case out.Failure != "":
		params.State = string(apitypes.MemorySnapshotStateFailed)
		failure := out.Failure
		if out.Unsupported {
			failure = unsupportedPrefix + failure
		}
		params.Failure = &failure
	default:
		params.State = string(apitypes.MemorySnapshotStateAvailable)
		params.SizeBytes, params.Sha256 = &out.SizeBytes, &out.SHA256
	}
	return pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		if _, err := e.queries.WithTx(tx).FinishSnapshot(ctx, params); errors.Is(err, pgx.ErrNoRows) {
			return ErrStaleSnapshot
		} else if err != nil {
			return fmt.Errorf("finish snapshot: %w", err)
		}
		return database.Notify(ctx, tx, database.ChannelContainerOp, out.Snapshot.String())
	})
}

// unsupportedPrefix marks a failure the host could not attempt, so the API
// answers `unsupported` instead of a failed snapshot.
const unsupportedPrefix = "unsupported: "

// SnapshotFailure maps a failed snapshot to the error its requester sees.
func SnapshotFailure(s Snapshot) error {
	if s.Failure == nil {
		return &ConflictError{Reason: "the snapshot failed"}
	}
	if reason, ok := strings.CutPrefix(*s.Failure, unsupportedPrefix); ok {
		return &UnsupportedError{Reason: reason}
	}
	return &ConflictError{Reason: "the snapshot failed: " + *s.Failure}
}

// SnapshotCommand asks a host to snapshot a container.
type SnapshotCommand struct {
	Snapshot  uuid.UUID
	Workspace identity.WorkspaceID
	Container ContainerID
	Deadline  time.Time
	// Ready is the probe a pod's automatic snapshot waits for.
	Ready *apitypes.CheckpointSpec
}

// PublishCommand asks a host to publish a container's filesystem as an
// image.
type PublishCommand struct {
	Request   uuid.UUID
	Workspace identity.WorkspaceID
	Container ContainerID
	Deadline  time.Time
}

// NetworkCommand gives a host a live container's outbound policy.
type NetworkCommand struct {
	Container ContainerID
	Policy    NetworkPolicy
}

// workloadCommands adds the host's snapshot, publish and network commands.
func (e *Execution) workloadCommands(ctx context.Context, host compute.HostID, out *HostCommands) error {
	snapshots, err := e.queries.PendingSnapshotsOnHost(ctx, hostUUID(host))
	if err != nil {
		return fmt.Errorf("list pending snapshots: %w", err)
	}
	for _, row := range snapshots {
		cmd := SnapshotCommand{
			Snapshot: row.ID, Workspace: identity.WorkspaceID(row.WorkspaceID), Container: ContainerID(row.ContainerID),
			Deadline: row.CreatedAt.Add(SnapshotDeadline),
		}
		if row.Automatic {
			var spec apitypes.FunctionSpec
			if err := json.Unmarshal(row.Spec, &spec); err != nil {
				return fmt.Errorf("decode release spec: %w", err)
			}
			if spec.Checkpoint != nil && spec.Pod != nil && spec.Checkpoint.ReadinessPath != nil {
				cmd.Ready = spec.Checkpoint
			}
		}
		out.Snapshot = append(out.Snapshot, cmd)
	}
	publishes, err := e.queries.PendingFilesystemImagesOnHost(ctx, hostUUID(host))
	if err != nil {
		return fmt.Errorf("list pending filesystem images: %w", err)
	}
	for _, row := range publishes {
		out.Publish = append(out.Publish, PublishCommand{
			Request: row.ID, Workspace: identity.WorkspaceID(row.WorkspaceID), Container: ContainerID(row.ContainerID),
			Deadline: row.CreatedAt.Add(PublishDeadline),
		})
	}
	policies, err := e.queries.NetworkPoliciesOnHost(ctx, hostUUID(host))
	if err != nil {
		return fmt.Errorf("list network policies: %w", err)
	}
	for _, row := range policies {
		out.Network = append(out.Network, NetworkCommand{
			Container: ContainerID(row.ID), Policy: NetworkPolicy{Block: row.BlockNetwork, Allow: row.AllowList},
		})
	}
	return nil
}

// Restore is the snapshot a starting container restores.
type Restore struct {
	Snapshot  uuid.UUID
	Workspace identity.WorkspaceID
	SHA256    string
	Automatic bool
}

// RestoreFor returns what a starting container restores, if anything.
func (e *Execution) RestoreFor(ctx context.Context, container ContainerID) (*Restore, error) {
	row, err := e.queries.RestoreSnapshot(ctx, uuid.UUID(container))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil //nolint:nilnil // No snapshot to restore is not an error.
	}
	if err != nil {
		return nil, fmt.Errorf("read restore snapshot: %w", err)
	}
	return &Restore{Snapshot: row.ID, Workspace: identity.WorkspaceID(row.WorkspaceID), SHA256: row.Sha256, Automatic: row.Automatic}, nil
}

// RestoreFailed stops offering an automatic snapshot a host could not
// restore; the container started cold.
func (e *Execution) RestoreFailed(ctx context.Context, snapshot uuid.UUID, reason string) error {
	if err := e.queries.RecordRestoreFailed(ctx, RecordRestoreFailedParams{ID: snapshot, Reason: reason}); err != nil {
		return fmt.Errorf("record restore failure: %w", err)
	}
	return nil
}

// TakeAutomaticSnapshots requests a snapshot of the first ready container
// of every checkpoint-enabled release that has none, and fails snapshots and
// publishes whose container stopped or deadline passed.
func (e *Execution) TakeAutomaticSnapshots(ctx context.Context, logger *slog.Logger) error {
	candidates, err := e.queries.AutomaticSnapshotCandidates(ctx)
	if err != nil {
		return fmt.Errorf("list snapshot candidates: %w", err)
	}
	for _, c := range candidates {
		err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
			q := e.queries.WithTx(tx)
			n, err := q.InsertAutomaticSnapshot(ctx, InsertAutomaticSnapshotParams{
				WorkspaceID: c.WorkspaceID, ReleaseID: c.ReleaseID, ContainerID: &c.ID,
			})
			if err != nil || n == 0 {
				return err //nolint:wrapcheck // Wrapped below.
			}
			route, err := q.ContainerRoute(ctx, c.ID)
			if err != nil || route.HostID == nil {
				return err //nolint:wrapcheck // Wrapped below.
			}
			return database.Notify(ctx, tx, database.ChannelHost, route.HostID.String())
		})
		if err != nil {
			logger.ErrorContext(ctx, "request automatic snapshot", "release_id", c.ReleaseID, "error", err)
		}
	}
	stale, err := e.queries.FailStaleSnapshots(ctx, SnapshotDeadline.Seconds())
	if err != nil {
		return fmt.Errorf("fail stale snapshots: %w", err)
	}
	staleImages, err := e.queries.FailStaleFilesystemImages(ctx, PublishDeadline.Seconds())
	if err != nil {
		return fmt.Errorf("fail stale filesystem images: %w", err)
	}
	ids := make([]string, 0, len(stale)+len(staleImages))
	for _, id := range append(stale, staleImages...) {
		ids = append(ids, id.String())
	}
	if len(ids) == 0 {
		return nil
	}
	return pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		return notifyAll(ctx, tx, database.ChannelContainerOp, ids)
	})
}

// FilesystemImage is a filesystem publish as its requester waits for it.
type FilesystemImage struct {
	ID      uuid.UUID
	State   string
	ImageID *string
	Failure *string
}

// CreateFilesystemImage records a publish of a ready container's
// filesystem and wakes its host.
func (e *Execution) CreateFilesystemImage(ctx context.Context, workspace identity.WorkspaceID, container ContainerID) (uuid.UUID, error) {
	var id uuid.UUID
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		q := e.queries.WithTx(tx)
		row, err := q.LockSnapshotSource(ctx, LockSnapshotSourceParams{ID: uuid.UUID(container), WorkspaceID: uuid.UUID(workspace)})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("lock container: %w", err)
		}
		if ContainerState(row.State) != ContainerReady || row.HostID == nil {
			return &ConflictError{Reason: "only a running container can be saved as an image"}
		}
		if id, err = q.InsertFilesystemImage(ctx, InsertFilesystemImageParams{WorkspaceID: uuid.UUID(workspace), ContainerID: row.ID}); err != nil {
			return fmt.Errorf("insert filesystem image: %w", err)
		}
		return database.Notify(ctx, tx, database.ChannelHost, row.HostID.String())
	})
	if err != nil {
		return uuid.Nil, fmt.Errorf("publish container %s: %w", container, err)
	}
	return id, nil
}

// WaitFilesystemImage waits until the publish is no longer in progress or
// ctx ends.
func (e *Execution) WaitFilesystemImage(ctx context.Context, listener *database.Listener, id uuid.UUID) (FilesystemImage, error) {
	wake, cancel := listener.Subscribe(database.ChannelContainerOp, id.String())
	defer cancel()
	poll := time.NewTicker(5 * time.Second)
	defer poll.Stop()
	for {
		row, err := e.queries.FilesystemImageView(ctx, id)
		if err != nil {
			return FilesystemImage{}, fmt.Errorf("read filesystem image: %w", err)
		}
		out := FilesystemImage{ID: row.ID, State: row.State, ImageID: row.ImageID, Failure: row.Failure}
		if row.State != "publishing" {
			return out, nil
		}
		select {
		case <-ctx.Done():
			return out, fmt.Errorf("wait for filesystem image: %w", ctx.Err())
		case <-wake:
		case <-poll.C:
		}
	}
}

// FinishFilesystemImage records the host's outcome once. register makes
// the published reference an image of the workspace, outside this
// transaction, and returns its id.
func (e *Execution) FinishFilesystemImage(ctx context.Context, host compute.HostID, container ContainerID, request uuid.UUID, failure string, register func(identity.WorkspaceID) (string, error)) error {
	params := FinishFilesystemImageParams{ID: request, ContainerID: uuid.UUID(container), HostID: hostUUID(host), State: "failed"}
	if failure == "" {
		view, err := e.queries.FilesystemImageView(ctx, request)
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrStaleSnapshot
		}
		if err != nil {
			return fmt.Errorf("read filesystem image: %w", err)
		}
		if view.State != "publishing" {
			return ErrStaleSnapshot
		}
		ws, err := e.workspaceOfContainer(ctx, e.queries, container)
		if err != nil {
			return err
		}
		if image, err := register(ws); err != nil {
			failure = "register image: " + err.Error()
		} else {
			params.State, params.ImageID = "published", &image
		}
	}
	if failure != "" {
		params.Failure = &failure
	}
	return pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		if _, err := e.queries.WithTx(tx).FinishFilesystemImage(ctx, params); errors.Is(err, pgx.ErrNoRows) {
			return ErrStaleSnapshot
		} else if err != nil {
			return fmt.Errorf("finish filesystem image: %w", err)
		}
		return database.Notify(ctx, tx, database.ChannelContainerOp, request.String())
	})
}

func (e *Execution) workspaceOfContainer(ctx context.Context, q *Queries, container ContainerID) (identity.WorkspaceID, error) {
	route, err := q.ContainerRoute(ctx, uuid.UUID(container))
	if err != nil {
		return identity.WorkspaceID{}, fmt.Errorf("read container: %w", err)
	}
	return identity.WorkspaceID(route.WorkspaceID), nil
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}
