// Package control owns apps, their workloads and immutable releases, and
// which release each workload runs. It asks execution for work through the
// database; it never changes tasks or containers.
package control

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"sort"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/schedules"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

// ErrNotFound means the app or function does not exist.
var ErrNotFound = errors.New("not found")

// InvalidSpecError rejects a function definition the schema cannot express.
type InvalidSpecError struct {
	Function string
	Reason   string
}

func (e *InvalidSpecError) Error() string {
	return fmt.Sprintf("function %s: %s", e.Function, e.Reason)
}

// SourceMissingError means a function names a source archive the workspace
// has not registered.
type SourceMissingError struct {
	Function string
	Sha256   string
}

func (e *SourceMissingError) Error() string {
	return fmt.Sprintf("function %s: source %s is not uploaded", e.Function, e.Sha256)
}

// Control is the control owner.
type Control struct {
	pool    *pgxpool.Pool
	queries *Queries
}

// NewControl returns the control owner over pool.
func NewControl(pool *pgxpool.Pool) *Control {
	return &Control{pool: pool, queries: New(pool)}
}

// Resolve fills every default of spec, so a release records the complete
// configuration it runs with and equal configurations have equal digests.
func Resolve(spec apitypes.FunctionSpec) (apitypes.FunctionSpec, error) {
	out := spec
	out.TimeoutSeconds = orDefault(spec.TimeoutSeconds, 3600)
	out.Concurrency = orDefault(spec.Concurrency, 1)
	out.MaxPendingTasks = orDefault(spec.MaxPendingTasks, 100)

	if r := spec.Resources; r.CpuLimitMillis != nil && *r.CpuLimitMillis < r.CpuMillis {
		return out, &InvalidSpecError{Function: spec.Name, Reason: "resources.cpu_limit_millis is below cpu_millis"}
	}
	if r := spec.Resources; r.MemoryLimitMib != nil && *r.MemoryLimitMib < r.MemoryMib {
		return out, &InvalidSpecError{Function: spec.Name, Reason: "resources.memory_limit_mib is below memory_mib"}
	}

	scaler := apitypes.Autoscaler{}
	if spec.Autoscaler != nil {
		scaler = *spec.Autoscaler
	}
	scaler.MinContainers = orDefault(scaler.MinContainers, 0)
	scaler.MaxContainers = orDefault(scaler.MaxContainers, 1)
	scaler.TasksPerContainer = orDefault(scaler.TasksPerContainer, 1)
	if *scaler.MinContainers > *scaler.MaxContainers {
		return out, &InvalidSpecError{Function: spec.Name, Reason: "autoscaler.min_containers exceeds max_containers"}
	}
	out.Autoscaler = &scaler

	retry := apitypes.RetryPolicy{MaxAttempts: 1}
	if spec.RetryPolicy != nil {
		retry = *spec.RetryPolicy
	}
	retry.DelaySeconds = orDefault(retry.DelaySeconds, 0)
	retry.Backoff = orDefault(retry.Backoff, apitypes.Fixed)
	out.RetryPolicy = &retry

	env := map[string]string{}
	if spec.Environment != nil {
		for k, v := range *spec.Environment {
			env[k] = v
		}
	}
	out.Environment = &env
	if err := resolveRuntime(spec, &out); err != nil {
		return out, err
	}
	return out, nil
}

func orDefault[T any](v *T, def T) *T {
	if v != nil {
		c := *v
		return &c
	}
	return &def
}

// Digest is the SHA-256 of a resolved spec's JSON. encoding/json writes
// struct fields in declaration order and map keys sorted, so the encoding is
// canonical for a given spec.
func Digest(resolved apitypes.FunctionSpec) ([]byte, []byte, error) {
	encoded, err := json.Marshal(resolved)
	if err != nil {
		return nil, nil, fmt.Errorf("encode spec: %w", err)
	}
	sum := sha256.Sum256(encoded)
	return encoded, sum[:], nil
}

type resolvedFunction struct {
	spec    apitypes.FunctionSpec
	encoded []byte
	digest  []byte
	source  storage.Digest
}

// Deploy makes the app's functions match req in one transaction. Each
// function whose resolved spec differs from its active release gets the next
// version, which becomes active in the same commit; an identical spec keeps
// the active release. With prune, unlisted functions stop.
func (c *Control) Deploy(ctx context.Context, workspace identity.WorkspaceID, app string, req apitypes.DeploymentRequest) (apitypes.Deployment, error) {
	functions := make([]resolvedFunction, len(req.Functions))
	seen := map[string]bool{}
	digests := make([][]byte, 0, len(req.Functions))
	for n, spec := range req.Functions {
		if seen[spec.Name] {
			return apitypes.Deployment{}, &InvalidSpecError{Function: spec.Name, Reason: "listed more than once"}
		}
		seen[spec.Name] = true
		resolved, err := Resolve(spec)
		if err != nil {
			return apitypes.Deployment{}, err
		}
		encoded, digest, err := Digest(resolved)
		if err != nil {
			return apitypes.Deployment{}, err
		}
		source, err := storage.ParseDigest(spec.Source.Sha256)
		if err != nil {
			return apitypes.Deployment{}, &InvalidSpecError{Function: spec.Name, Reason: "source.sha256 is not a lowercase hex digest"}
		}
		functions[n] = resolvedFunction{spec: resolved, encoded: encoded, digest: digest, source: source}
		digests = append(digests, source[:])
	}
	// Workloads lock in name order, although the app row already
	// serializes deploys of one app.
	order := make([]int, len(functions))
	for n := range order {
		order[n] = n
	}
	sort.Slice(order, func(a, b int) bool { return functions[order[a]].spec.Name < functions[order[b]].spec.Name })

	var out apitypes.Deployment
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		appRow, err := q.UpsertApp(ctx, UpsertAppParams{WorkspaceID: uuid.UUID(workspace), Name: app})
		if err != nil {
			return fmt.Errorf("upsert app: %w", err)
		}
		registered, err := q.RegisteredSources(ctx, RegisteredSourcesParams{WorkspaceID: uuid.UUID(workspace), Digests: digests})
		if err != nil {
			return fmt.Errorf("read registered sources: %w", err)
		}
		have := map[string]bool{}
		for _, d := range registered {
			have[string(d)] = true
		}
		for _, f := range functions {
			if !have[string(f.source[:])] {
				return &SourceMissingError{Function: f.spec.Name, Sha256: f.source.String()}
			}
		}

		releases := make([]apitypes.Release, len(functions))
		for _, n := range order {
			release, err := c.deployFunction(ctx, tx, q, appRow.ID, functions[n])
			if err != nil {
				return err
			}
			releases[n] = release
		}

		pruned := []string{}
		if req.Prune != nil && *req.Prune {
			keep := make([]string, len(functions))
			for n, f := range functions {
				keep[n] = f.spec.Name
			}
			rows, err := q.PruneFunctions(ctx, PruneFunctionsParams{AppID: appRow.ID, Keep: keep})
			if err != nil {
				return fmt.Errorf("prune functions: %w", err)
			}
			for _, row := range rows {
				pruned = append(pruned, row.Name)
				if row.ActiveReleaseID != nil {
					if err := database.Notify(ctx, tx, database.ChannelExecution, row.ActiveReleaseID.String()); err != nil {
						return err
					}
				}
			}
			sort.Strings(pruned)
		}
		out = apitypes.Deployment{
			App: apitypes.App{
				Id: appRow.ID, Name: appRow.Name, State: apitypes.AppState(appRow.State), CreatedAt: appRow.CreatedAt,
			},
			Releases: releases,
			Pruned:   pruned,
		}
		return nil
	})
	if err != nil {
		return apitypes.Deployment{}, fmt.Errorf("deploy %s: %w", app, err)
	}
	return out, nil
}

// deployFunction upserts and locks the workload, then keeps or replaces its
// active release. Execution is woken for the new release and for the one it
// replaces, which drains once its work finishes.
func (c *Control) deployFunction(ctx context.Context, tx pgx.Tx, q *Queries, app uuid.UUID, f resolvedFunction) (apitypes.Release, error) {
	workload, err := q.UpsertWorkload(ctx, UpsertWorkloadParams{AppID: app, Name: f.spec.Name})
	if err != nil {
		return apitypes.Release{}, fmt.Errorf("upsert workload %s: %w", f.spec.Name, err)
	}
	if err := schedules.Apply(ctx, tx, workload.ID, f.spec.Cron); err != nil {
		return apitypes.Release{}, fmt.Errorf("schedule %s: %w", f.spec.Name, err)
	}
	if workload.ActiveReleaseID != nil {
		active, err := q.ActiveRelease(ctx, *workload.ActiveReleaseID)
		if err != nil {
			return apitypes.Release{}, fmt.Errorf("read active release of %s: %w", f.spec.Name, err)
		}
		// A reused release may have been stopped; a replaced one drains.
		if err := database.Notify(ctx, tx, database.ChannelExecution, active.ID.String()); err != nil {
			return apitypes.Release{}, err
		}
		if bytes.Equal(active.SpecDigest, f.digest) {
			return apitypes.Release{
				Id: active.ID, Function: f.spec.Name, Version: int(active.Version), CreatedAt: active.CreatedAt, Spec: f.spec,
			}, nil
		}
	}
	inserted, err := q.InsertRelease(ctx, InsertReleaseParams{
		WorkloadID: workload.ID, Version: workload.NextVersion,
		Spec: f.encoded, SpecDigest: f.digest, SourceSha256: f.source[:],
	})
	if err != nil {
		return apitypes.Release{}, fmt.Errorf("insert release of %s: %w", f.spec.Name, err)
	}
	if err := q.ActivateRelease(ctx, ActivateReleaseParams{ReleaseID: inserted.ID, ID: workload.ID}); err != nil {
		return apitypes.Release{}, fmt.Errorf("activate release of %s: %w", f.spec.Name, err)
	}
	if err := database.Notify(ctx, tx, database.ChannelExecution, inserted.ID.String()); err != nil {
		return apitypes.Release{}, err
	}
	return apitypes.Release{
		Id: inserted.ID, Function: f.spec.Name, Version: int(inserted.Version), CreatedAt: inserted.CreatedAt, Spec: f.spec,
	}, nil
}

// GetFunction returns a function and its active release.
func (c *Control) GetFunction(ctx context.Context, workspace identity.WorkspaceID, app, name string) (apitypes.Function, error) {
	row, err := c.queries.FunctionRelease(ctx, FunctionReleaseParams{WorkspaceID: uuid.UUID(workspace), AppName: app, Name: name})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.Function{}, ErrNotFound
	}
	if err != nil {
		return apitypes.Function{}, fmt.Errorf("read function: %w", err)
	}
	var spec apitypes.FunctionSpec
	if err := json.Unmarshal(row.Spec, &spec); err != nil {
		return apitypes.Function{}, fmt.Errorf("decode release spec: %w", err)
	}
	return apitypes.Function{
		Name:  row.Name,
		App:   row.AppName,
		State: apitypes.FunctionState(row.DesiredState),
		ActiveRelease: apitypes.Release{
			Id: row.ID, Function: row.Name, Version: int(row.Version), CreatedAt: row.CreatedAt, Spec: spec,
		},
	}, nil
}
