package control

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

// PreviewLease is how long a preview lives without a renewal. Following its
// output renews it, so a closed terminal stops it within this long.
const PreviewLease = time.Minute

// Preview is a release that runs one container for `lazycloud serve`. It
// takes a negative version, so it is never deployed or active.
type Preview struct {
	Release   uuid.UUID
	App, Name string
	Kind      apitypes.PreviewKind
	Spec      apitypes.WorkloadSpec
	// Live is false once the preview was stopped, its lease lapsed or its
	// timeout passed.
	Live           bool
	LeaseExpiresAt time.Time
	DeadlineAt     *time.Time
	CreatedAt      time.Time
	// LoadError is why the handler failed to import in the preview's
	// container; no container of the preview starts after it.
	LoadError *string
}

// PreviewKind is what a definition previews as.
func PreviewKind(spec apitypes.WorkloadSpec) apitypes.PreviewKind {
	if spec.Http == nil {
		return apitypes.PreviewKindFunction
	}
	return apitypes.PreviewKind(spec.Http.Kind)
}

// CreatePreview records a preview release of spec in app and its lease in
// one transaction. A workload never deployed is created stopped, so its
// preview serves without deploying it.
func (c *Control) CreatePreview(ctx context.Context, workspace identity.WorkspaceID, user identity.UserID, app string, spec apitypes.WorkloadSpec, timeoutSeconds int) (Preview, error) {
	if spec.Pod != nil {
		return Preview{}, &InvalidSpecError{Function: spec.Name, Reason: "pods, devboxes and sandboxes are not served; deploy or create them"}
	}
	resolved, err := Resolve(spec)
	if err != nil {
		return Preview{}, err
	}
	encoded, digest, err := Digest(resolved)
	if err != nil {
		return Preview{}, err
	}
	source, err := storage.ParseDigest(spec.Source.Sha256)
	if err != nil {
		return Preview{}, &InvalidSpecError{Function: spec.Name, Reason: "source.sha256 is not a lowercase hex digest"}
	}
	out := Preview{App: app, Name: spec.Name, Kind: PreviewKind(resolved), Spec: resolved, Live: true}
	err = pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		appRow, err := q.UpsertApp(ctx, UpsertAppParams{WorkspaceID: uuid.UUID(workspace), Name: app})
		if err != nil {
			return fmt.Errorf("upsert app: %w", err)
		}
		registered, err := q.RegisteredSources(ctx, RegisteredSourcesParams{WorkspaceID: uuid.UUID(workspace), Digests: [][]byte{source[:]}})
		if err != nil {
			return fmt.Errorf("read registered sources: %w", err)
		}
		if len(registered) == 0 {
			return &SourceMissingError{Function: spec.Name, Sha256: source.String()}
		}
		workload, err := q.EnsurePreviewWorkload(ctx, EnsurePreviewWorkloadParams{AppID: appRow.ID, Kind: string(resolved.Kind), Name: spec.Name})
		if err != nil {
			return fmt.Errorf("ensure workload: %w", err)
		}
		release, err := q.InsertPreviewRelease(ctx, InsertPreviewReleaseParams{
			WorkloadID: workload, Spec: encoded, SpecDigest: digest, SourceSha256: source[:],
		})
		if err != nil {
			return fmt.Errorf("insert preview release: %w", err)
		}
		lease, err := q.InsertPreview(ctx, InsertPreviewParams{
			ReleaseID: release.ID, WorkspaceID: uuid.UUID(workspace), UserID: uuid.UUID(user), Kind: string(out.Kind),
			LeaseSeconds: PreviewLease.Seconds(), TimeoutSeconds: int32(timeoutSeconds), //nolint:gosec // the API bounds the timeout
		})
		if err != nil {
			return fmt.Errorf("insert preview: %w", err)
		}
		out.Release, out.LeaseExpiresAt, out.DeadlineAt, out.CreatedAt = release.ID, lease.LeaseExpiresAt, lease.DeadlineAt, lease.CreatedAt
		return database.Notify(ctx, tx, database.ChannelExecution, release.ID.String())
	})
	if err != nil {
		return Preview{}, fmt.Errorf("create preview of %s: %w", spec.Name, err)
	}
	return out, nil
}

// GetPreview reads a preview of the workspace.
func (c *Control) GetPreview(ctx context.Context, workspace identity.WorkspaceID, release uuid.UUID) (Preview, error) {
	row, err := c.queries.PreviewRow(ctx, PreviewRowParams{ReleaseID: release, WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return Preview{}, ErrNotFound
	}
	if err != nil {
		return Preview{}, fmt.Errorf("read preview: %w", err)
	}
	out := Preview{
		Release: row.ReleaseID, App: row.AppName, Name: row.Name, Kind: apitypes.PreviewKind(row.Kind), Live: row.Live,
		LeaseExpiresAt: row.LeaseExpiresAt, DeadlineAt: row.DeadlineAt, CreatedAt: row.CreatedAt, LoadError: row.LoadError,
	}
	if err := json.Unmarshal(row.Spec, &out.Spec); err != nil {
		return Preview{}, fmt.Errorf("decode preview spec: %w", err)
	}
	return out, nil
}

// RenewPreview extends a live preview's lease and reports whether it is
// still live.
func (c *Control) RenewPreview(ctx context.Context, release uuid.UUID) (bool, error) {
	_, err := c.queries.RenewPreview(ctx, RenewPreviewParams{ReleaseID: release, LeaseSeconds: PreviewLease.Seconds()})
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("renew preview: %w", err)
	}
	return true, nil
}

// StopPreview stops a preview; planning drains its container.
func (c *Control) StopPreview(ctx context.Context, workspace identity.WorkspaceID, release uuid.UUID) error {
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		if err := c.queries.WithTx(tx).StopPreview(ctx, StopPreviewParams{ReleaseID: release, WorkspaceID: uuid.UUID(workspace)}); err != nil {
			return fmt.Errorf("stop preview: %w", err)
		}
		return database.Notify(ctx, tx, database.ChannelExecution, release.String())
	})
	if err != nil {
		return fmt.Errorf("stop preview %s: %w", release, err)
	}
	return nil
}
