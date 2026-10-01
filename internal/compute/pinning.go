package compute

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// PinnedMachine returns the machine a spec pins, or "" when it pins none.
func PinnedMachine(spec apitypes.FunctionSpec) string {
	if spec.Placement == nil || spec.Placement.Machine == nil {
		return ""
	}
	return *spec.Placement.Machine
}

// CheckMachineServes refuses a pin to a machine that does not exist or does
// not serve the workspace. A deployment pinned to a machine that exists
// waits for it while it is offline.
func CheckMachineServes(ctx context.Context, db DBTX, workspace uuid.UUID, machine string) error {
	_, err := New(db).PinnedMachine(ctx, PinnedMachineParams{WorkspaceID: workspace, Name: machine})
	if errors.Is(err, pgx.ErrNoRows) {
		return notServing(ctx, db, workspace, machine)
	}
	if err != nil {
		return fmt.Errorf("read pinned machine: %w", err)
	}
	return nil
}

// CheckMachineRuns refuses a run pinned to a machine that is missing or
// cannot take work now: a run fails at once rather than waiting for the
// machine, and never falls back to other capacity.
func CheckMachineRuns(ctx context.Context, db DBTX, workspace uuid.UUID, machine string) error {
	row, err := New(db).PinnedMachine(ctx, PinnedMachineParams{WorkspaceID: workspace, Name: machine})
	if errors.Is(err, pgx.ErrNoRows) {
		return notServing(ctx, db, workspace, machine)
	}
	if err != nil {
		return fmt.Errorf("read pinned machine: %w", err)
	}
	fresh := row.LastSeenAt != nil && time.Since(*row.LastSeenAt) < LivenessTimeout
	if Phase(row.Phase) != PhaseReady || HostState(row.State) != HostOnline || !fresh ||
		CapacityState(row.CapacityState) != CapacityAvailable {
		return &ConflictError{Message: fmt.Sprintf(
			"machine %q is offline or not taking work (%s); runs pinned to it fail until it is ready", machine, row.Phase)}
	}
	return nil
}

func notServing(ctx context.Context, db DBTX, workspace uuid.UUID, machine string) error {
	name, err := New(db).WorkspaceName(ctx, workspace)
	if err != nil {
		return fmt.Errorf("read workspace name: %w", err)
	}
	return &InvalidError{Message: fmt.Sprintf(
		"machine %q is not joined to this account or does not serve workspace %q", machine, name)}
}

// CheckMachineServes is CheckMachineServes over the owner's pool.
func (c *Compute) CheckMachineServes(ctx context.Context, workspace identity.WorkspaceID, machine string) error {
	return CheckMachineServes(ctx, c.pool, uuid.UUID(workspace), machine)
}
