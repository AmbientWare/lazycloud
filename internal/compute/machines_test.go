package compute_test

import (
	"errors"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

func machineConfig() compute.Config {
	return compute.Config{InstallURL: "https://lazycloud.test", ServerAddress: "hosts.lazycloud.test:443", ServerTLS: true}
}

var machineCapacity = compute.Capacity{CPUMillis: 8000, MemoryBytes: 16 * gib} //nolint:gochecknoglobals // Test constant.

// joinMachine mints a join command for gpu-1 and enrolls a host with it,
// then opens its first session.
func joinMachine(t *testing.T, o owners, account identity.UserID, workspaces ...uuid.UUID) (compute.HostID, string) {
	t.Helper()
	ctx := t.Context()
	cmd, err := o.compute.CreateMachineJoin(ctx, compute.MachineJoin{Account: account, Name: "gpu-1", Workspaces: workspaces})
	if err != nil {
		t.Fatal(err)
	}
	host, token, err := o.compute.Enroll(ctx, joinToken(t, cmd), compute.HostReport{Hostname: "gpu-1", Capacity: machineCapacity})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := o.compute.OpenSession(ctx, host, compute.SessionOpen{BootID: "boot", Capacity: machineCapacity}); err != nil {
		t.Fatal(err)
	}
	return host, token
}

// gpu1 is the account's machine gpu-1, or nil.
func gpu1(t *testing.T, o owners, account identity.UserID) *compute.Machine {
	t.Helper()
	machines, err := o.compute.AccountMachines(t.Context(), account, "", 100)
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range machines {
		if m.Name == "gpu-1" {
			return &m
		}
	}
	return nil
}

func TestMachineJoinEnrollsOnceUnderAnUnambiguousName(t *testing.T) {
	ctx := t.Context()
	o := newOwners(t, machineConfig())
	alice, bob := newUser(t, o.pool, "alice@example.com"), newUser(t, o.pool, "bob@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	join := compute.MachineJoin{Account: alice, Name: "gpu-1", Workspaces: []uuid.UUID{dev}}

	var unavailable *compute.UnavailableError
	if _, err := o.compute.CreateMachineJoin(ctx, join); !errors.As(err, &unavailable) {
		t.Fatalf("join without a published agent release: %v, want UnavailableError", err)
	}
	publish(t, o.compute)

	first, err := o.compute.CreateMachineJoin(ctx, join)
	if err != nil {
		t.Fatal(err)
	}
	second, err := o.compute.CreateMachineJoin(ctx, join)
	if err != nil {
		t.Fatal(err)
	}
	if second.Machine.ID != first.Machine.ID {
		t.Fatalf("a second join of a machine that never joined made machine %s, want %s reused", second.Machine.ID, first.Machine.ID)
	}
	if _, _, err := o.compute.Enroll(ctx, joinToken(t, first), compute.HostReport{Hostname: "gpu-1", Capacity: machineCapacity}); !errors.Is(err, compute.ErrInvalidJoinToken) {
		t.Fatalf("replaced join token: %v, want ErrInvalidJoinToken", err)
	}
	host, token, err := o.compute.Enroll(ctx, joinToken(t, second), compute.HostReport{Hostname: "gpu-1", Capacity: machineCapacity})
	if err != nil {
		t.Fatal(err)
	}
	if host != first.Machine.ID {
		t.Fatalf("enrolled host %s, want the machine %s", host, first.Machine.ID)
	}
	if m := gpu1(t, o, alice); m == nil || m.Phase != compute.PhaseJoining {
		t.Fatalf("enrolled machine %+v, want joining", m)
	}
	if _, err := o.compute.OpenSession(ctx, host, compute.SessionOpen{BootID: "boot", Capacity: machineCapacity}); err != nil {
		t.Fatal(err)
	}
	if m := gpu1(t, o, alice); m == nil || m.Phase != compute.PhaseReady || !m.Schedulable() {
		t.Fatalf("machine after its first session %+v, want ready and schedulable", m)
	}
	if got, err := o.compute.AuthenticateHost(ctx, token); err != nil || got != host {
		t.Fatalf("host token: %v %v", got, err)
	}

	var conflict *compute.ConflictError
	if _, err := o.compute.CreateMachineJoin(ctx, join); !errors.As(err, &conflict) {
		t.Fatalf("join of a joined name: %v, want ConflictError", err)
	}
	if _, err := o.compute.CreateMachineJoin(ctx, compute.MachineJoin{Account: bob, Name: "gpu-1", Workspaces: []uuid.UUID{dev}}); !errors.As(err, &conflict) {
		t.Fatalf("another account's gpu-1 serving dev: %v, want ConflictError", err)
	}
}

func TestMachineFailingAPreflightCheckNeverTakesWork(t *testing.T) {
	ctx := t.Context()
	o := newOwners(t, machineConfig())
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	publish(t, o.compute)
	cmd, err := o.compute.CreateMachineJoin(ctx, compute.MachineJoin{Account: alice, Name: "gpu-1", Workspaces: []uuid.UUID{dev}})
	if err != nil {
		t.Fatal(err)
	}
	host, _, err := o.compute.Enroll(ctx, joinToken(t, cmd), compute.HostReport{
		Hostname: "gpu-1", Capacity: machineCapacity,
		Preflight: []compute.PreflightCheck{
			{Name: "docker", OK: false, Severity: "error", Message: "Docker is not running", Remediation: "systemctl start docker"},
			{Name: "disk", OK: false, Severity: "warning", Message: "Disk is nearly full"},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := o.compute.OpenSession(ctx, host, compute.SessionOpen{BootID: "boot", Capacity: machineCapacity}); err != nil {
		t.Fatal(err)
	}
	m := gpu1(t, o, alice)
	if m == nil || m.Phase != compute.PhaseFailed || m.Failure == nil || *m.Failure != compute.FailurePreflight ||
		m.PhaseMessage != "Docker is not running" || len(m.Remediation()) != 1 {
		t.Fatalf("machine %+v, want failed host_preflight_failed naming the error check", m)
	}
	pinned := newRelease(t, o.pool, dev, `{"placement": {"machine": "gpu-1"}}`)
	container := pendingContainer(t, o.pool, dev, pinned, 1000, gib)
	if n := place(t, o); n != 0 {
		t.Fatalf("placed %d containers on a machine that failed its checks", n)
	}
	if h := containerHost(t, o.pool, container); h != nil {
		t.Fatalf("pinned container assigned to %s", h)
	}

	// A failed machine takes a new join command under its name.
	again, err := o.compute.CreateMachineJoin(ctx, compute.MachineJoin{Account: alice, Name: "gpu-1", Workspaces: []uuid.UUID{dev}})
	if err != nil {
		t.Fatalf("rejoin a failed machine: %v", err)
	}
	if again.Machine.ID != m.ID || again.Machine.Phase != compute.PhaseRequested {
		t.Fatalf("rejoined machine %+v, want %s requested again", again.Machine, m.ID)
	}
}

func TestPinnedWorkRunsOnlyOnItsMachineAndUnpinnedWorkNever(t *testing.T) {
	o := newOwners(t, machineConfig())
	alice := newUser(t, o.pool, "alice@example.com")
	dev, prod := newWorkspace(t, o.pool, "dev", alice), newWorkspace(t, o.pool, "prod", alice)
	publish(t, o.compute)
	machine, _ := joinMachine(t, o, alice, dev)
	platform := newHost(t, o.pool, hostSpec{CPU: 1000, Memory: 2 * gib})

	pinned := pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{"placement": {"machine": "gpu-1"}}`), 1000, gib)
	elsewhere := pendingContainer(t, o.pool, prod, newRelease(t, o.pool, prod, `{"placement": {"machine": "gpu-1"}}`), 1000, gib)
	unpinned := newRelease(t, o.pool, dev, `{}`)
	first := pendingContainer(t, o.pool, dev, unpinned, 1000, gib)
	overflow := pendingContainer(t, o.pool, dev, unpinned, 1000, gib)

	if n := place(t, o); n != 2 {
		t.Fatalf("placed %d, want the pinned container and one unpinned", n)
	}
	if h := containerHost(t, o.pool, pinned); h == nil || *h != uuid.UUID(machine) {
		t.Fatalf("pinned container on %v, want the machine %s", h, machine)
	}
	if h := containerHost(t, o.pool, first); h == nil || *h != uuid.UUID(platform) {
		t.Fatalf("unpinned container on %v, want the platform host", h)
	}
	if h := containerHost(t, o.pool, overflow); h != nil {
		t.Fatalf("unpinned container that fits no platform host went to %s; the machine has room but takes only pinned work", h)
	}
	if h := containerHost(t, o.pool, elsewhere); h != nil {
		t.Fatalf("container pinned from a workspace the machine does not serve went to %s", h)
	}
}

func TestMachineKeepsWorkspacesWhoseDeploymentsPinIt(t *testing.T) {
	ctx := t.Context()
	o := newOwners(t, machineConfig())
	alice := newUser(t, o.pool, "alice@example.com")
	dev, prod, lab := newWorkspace(t, o.pool, "dev", alice), newWorkspace(t, o.pool, "prod", alice), newWorkspace(t, o.pool, "lab", alice)
	publish(t, o.compute)
	joinMachine(t, o, alice, dev, prod)
	newRelease(t, o.pool, dev, `{"placement": {"machine": "gpu-1"}}`)

	var conflict *compute.ConflictError
	if _, err := o.compute.UpdateMachineWorkspaces(ctx, alice, "gpu-1", []uuid.UUID{prod}); !errors.As(err, &conflict) {
		t.Fatalf("drop dev while its deployment pins the machine: %v, want ConflictError", err)
	}
	m, err := o.compute.UpdateMachineWorkspaces(ctx, alice, "gpu-1", []uuid.UUID{dev, lab})
	if err != nil {
		t.Fatalf("drop prod, which pins nothing: %v", err)
	}
	if len(m.Workspaces) != 2 || m.Workspaces[0] != "dev" || m.Workspaces[1] != "lab" {
		t.Fatalf("machine serves %v, want dev and lab", m.Workspaces)
	}
}

func TestRemovingAMachineStopsItsWorkAndRevokesItsCredential(t *testing.T) {
	ctx := t.Context()
	o := newOwners(t, machineConfig())
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	publish(t, o.compute)
	host, token := joinMachine(t, o, alice, dev)
	release := newRelease(t, o.pool, dev, `{"placement": {"machine": "gpu-1"}}`)
	w := runningAttempt(t, o.pool, dev, release, host)

	if err := o.compute.RemoveMachine(ctx, alice, "gpu-1"); err != nil {
		t.Fatal(err)
	}
	assertRetried(t, o.pool, w)
	if _, err := o.compute.AuthenticateHost(ctx, token); !errors.Is(err, compute.ErrUnknownHost) {
		t.Fatalf("removed machine's token: %v, want ErrUnknownHost", err)
	}
	if _, err := o.compute.OpenSession(ctx, host, compute.SessionOpen{BootID: "boot", Capacity: machineCapacity}); !errors.Is(err, compute.ErrUnknownHost) {
		t.Fatalf("removed machine's session: %v, want ErrUnknownHost", err)
	}
	if m := gpu1(t, o, alice); m != nil {
		t.Fatalf("removed machine still listed: %+v", m)
	}
	if err := o.compute.RemoveMachine(ctx, alice, host.String()); err != nil {
		t.Fatalf("remove a removed machine: %v", err)
	}
	if phase, _ := hostPhase(t, o.pool, host); phase != string(compute.PhaseDeleted) {
		t.Fatalf("removed machine is %s, want deleted", phase)
	}
	// The name is free again.
	if again, _ := joinMachine(t, o, alice, dev); again == host {
		t.Fatal("a new join reused the removed machine")
	}
}
