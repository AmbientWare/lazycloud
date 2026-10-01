package control

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// PrepareRelease returns a release of the function that runs spec, for calls
// from a working tree. A release of the function with the same resolved spec
// is reused, the active one first, so unchanged code reaches the deployed
// containers; a stopped workload reuses only working-tree releases. Otherwise an unversioned release is inserted; it is never
// active and its tasks name it explicitly. A missing app or function is
// created without being deployed.
func (c *Control) PrepareRelease(ctx context.Context, workspace identity.WorkspaceID, app, function string, spec apitypes.FunctionSpec) (apitypes.Release, error) {
	if spec.Name != function {
		return apitypes.Release{}, &InvalidSpecError{Function: spec.Name, Reason: fmt.Sprintf("the path names function %s", function)}
	}
	f, err := resolveFunction(spec)
	if err != nil {
		return apitypes.Release{}, err
	}
	var out apitypes.Release
	err = pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		appRow, err := q.UpsertApp(ctx, UpsertAppParams{WorkspaceID: uuid.UUID(workspace), Name: app})
		if err != nil {
			return fmt.Errorf("upsert app: %w", err)
		}
		if err := requireSources(ctx, q, workspace, []resolvedFunction{f}); err != nil {
			return err
		}
		workload, err := q.EnsureWorkload(ctx, EnsureWorkloadParams{AppID: appRow.ID, Kind: string(KindOf(f.spec)), Name: function})
		if err != nil {
			return fmt.Errorf("ensure workload: %w", err)
		}
		// Execution admits tasks to a deployed version only while the
		// workload is active, so a stopped workload's calls need a
		// working-tree release even for unchanged code.
		existing, err := q.ReleaseByDigest(ctx, ReleaseByDigestParams{
			WorkloadID: workload.ID, SpecDigest: f.digest, ActiveReleaseID: workload.ActiveReleaseID,
			UnversionedOnly: WorkloadState(workload.DesiredState) != WorkloadActive,
		})
		if err == nil {
			out = apitypes.Release{
				Id: existing.ID, Function: function, Version: versionOf(existing.Version), CreatedAt: existing.CreatedAt, Spec: f.spec,
			}
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return fmt.Errorf("find release: %w", err)
		}
		inserted, err := q.InsertRelease(ctx, InsertReleaseParams{
			WorkloadID: workload.ID, Spec: f.encoded, SpecDigest: f.digest, SourceSha256: f.source[:],
		})
		if err != nil {
			return fmt.Errorf("insert release: %w", err)
		}
		out = apitypes.Release{Id: inserted.ID, Function: function, CreatedAt: inserted.CreatedAt, Spec: f.spec}
		return nil
	})
	if err != nil {
		return apitypes.Release{}, fmt.Errorf("prepare %s.%s: %w", app, function, err)
	}
	return out, nil
}
