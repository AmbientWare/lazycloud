package compute_test

import (
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// A run pinned to a machine fails at admission while the machine is missing
// or offline, and never waits or falls back; a deployed call pinned to the
// same offline machine is admitted and waits for it.
func TestRunsPinnedToAnUnavailableMachineFailAtOnce(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o := newOwners(t, machineConfig())
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	publish(t, o.compute)
	spec := `{"max_pending_tasks": 10, "resources": {"cpu_millis": 1000, "memory_mib": 1024}, "placement": {"machine": "gpu-1"}}`
	deployed := deploy(t, o.pool, dev, "train", spec)
	runRelease := scan[uuid.UUID](t, o.pool, `
insert into releases (workload_id, version, spec, spec_digest, source_sha256)
select workload_id, null, spec, sha256('run'), sha256('src') from releases where id = $1 returning id`, deployed)

	submit := func(release *uuid.UUID) error {
		_, err := o.execution.Submit(ctx, execution.SubmitRequest{
			Workspace: identity.WorkspaceID(dev), App: "train", Function: "f", Release: release,
			Inputs: []execution.TaskInput{{Payload: execution.Payload{Encoding: execution.EncodingJSON, Data: []byte(`{"args": [], "kwargs": {}}`)}}},
		})
		return err
	}
	if err := submit(&runRelease); apitypes.CodeOf(err) != apitypes.InvalidRequest {
		t.Fatalf("run pinned to a machine that never joined: %v, want Invalid", err)
	}
	if err := o.compute.CheckMachineServes(ctx, identity.WorkspaceID(dev), "gpu-1"); apitypes.CodeOf(err) != apitypes.InvalidRequest {
		t.Fatalf("deploy pinned to a machine that never joined: %v, want Invalid", err)
	}

	host, _ := joinMachine(t, o, alice, dev)
	if err := submit(&runRelease); err != nil {
		t.Fatalf("run pinned to a ready machine: %v", err)
	}

	run(t, o.pool, "update hosts set last_seen_at = now() - interval '1 minute' where id = $1", uuid.UUID(host))
	if err := submit(&runRelease); apitypes.CodeOf(err) != apitypes.Conflict {
		t.Fatalf("run pinned to an offline machine: %v, want Conflict", err)
	}
	if err := submit(nil); err != nil {
		t.Fatalf("deployed call pinned to an offline machine: %v, want it admitted to wait", err)
	}
	if err := o.compute.CheckMachineServes(ctx, identity.WorkspaceID(dev), "gpu-1"); err != nil {
		t.Fatalf("deploy pinned to an offline machine that exists: %v", err)
	}
}
