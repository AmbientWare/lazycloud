package execution

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/netip"
	"slices"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// Pods, devboxes and sandboxes are workloads whose release runs a command.
// Their containers are ordinary containers: a pod's serve containers follow
// its count and connections, and instances and shell containers are started
// on request and stop once idle.

// ContainerPurpose says who decides a container exists.
type ContainerPurpose string

const (
	// PurposeServe containers are the ones planning counts for a release.
	PurposeServe ContainerPurpose = "serve"
	// PurposeInstance containers were started on request (Pod.create,
	// Sandbox.create) and stop once idle.
	PurposeInstance ContainerPurpose = "instance"
	// PurposeShell containers idle for shells and stop shortly after the
	// last one closes.
	PurposeShell ContainerPurpose = "shell"
)

// InvalidError is a request the workload cannot satisfy as asked.
type InvalidError struct{ Reason string }

func (e *InvalidError) Error() string { return e.Reason }

// ConflictError is a request the container or workload's state refuses.
type ConflictError struct{ Reason string }

func (e *ConflictError) Error() string { return e.Reason }

const (
	// shellKeepWarm is how long a shell container outlives its last shell.
	shellKeepWarm = 30 * time.Second
	// podWakeWindow is how long a wake keeps a pod's container wanted
	// without a connection.
	podWakeWindow = 15 * time.Minute
	// idleBatch bounds the idle instances one pass stops.
	idleBatch = 200
	// LeaseTTL is how long a connection keeps a container active without a
	// renewal; holders renew well within it.
	LeaseTTL = 30 * time.Second
)

// InstanceRequest starts an instance: of release, or of the release of the
// memory snapshot it restores.
type InstanceRequest struct {
	Release  *uuid.UUID
	Snapshot *uuid.UUID
	Command  []string
	// Timeout is the idle lifetime in seconds; nil uses the release's
	// keep-warm window, and 0 or -1 keeps it up until it is stopped.
	Timeout *int
	Shell   bool
}

// Instance is a container started on request, as its caller sees it.
type Instance struct {
	ID          ContainerID
	Release     uuid.UUID
	App         string
	Name        string
	Kind        apitypes.WorkloadKind
	State       ContainerState
	Purpose     ContainerPurpose
	KeepWarm    *int
	ActiveUntil *time.Time
	StopReason  *StopReason
	ExitMessage *string
	ExitCode    *int
	CreatedAt   time.Time
	ReadyAt     *time.Time
	Host        *uuid.UUID
	// Ports are the ports exposed through the edge: the release's and those
	// exposed since.
	Ports []int
	Spec  apitypes.FunctionSpec
}

// CreateInstance admits and records a pending instance, which placement
// starts like any container.
func (e *Execution) CreateInstance(ctx context.Context, workspace identity.WorkspaceID, req InstanceRequest) (Instance, error) {
	var id uuid.UUID
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		q := e.queries.WithTx(tx)
		release := req.Release
		if req.Snapshot != nil {
			snap, err := q.SnapshotOfWorkspace(ctx, SnapshotOfWorkspaceParams{ID: *req.Snapshot, WorkspaceID: uuid.UUID(workspace)})
			if errors.Is(err, pgx.ErrNoRows) {
				return ErrNotFound
			}
			if err != nil {
				return fmt.Errorf("read snapshot: %w", err)
			}
			if snap.State != string(apitypes.MemorySnapshotStateAvailable) {
				return &ConflictError{Reason: "the memory snapshot is not available"}
			}
			if release != nil && *release != snap.ReleaseID {
				return &InvalidError{Reason: "the snapshot belongs to another release"}
			}
			release = &snap.ReleaseID
		}
		if release == nil {
			return &InvalidError{Reason: "an instance needs a release_id or a snapshot_id"}
		}
		row, err := q.InstanceRelease(ctx, InstanceReleaseParams{ReleaseID: *release, WorkspaceID: uuid.UUID(workspace)})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("read release: %w", err)
		}
		if !row.Live {
			return &ConflictError{Reason: "the workload is deleted or its app is paused"}
		}
		var spec apitypes.FunctionSpec
		if err := json.Unmarshal(row.Spec, &spec); err != nil {
			return fmt.Errorf("decode release spec: %w", err)
		}
		params, err := instanceParams(workspace, row, spec, req)
		if err != nil {
			return err
		}
		// An instance has nowhere else to run, so the account's container
		// limit refuses it rather than queueing it.
		grant, err := billing.Admit(ctx, tx, billing.Request{
			Workspace: uuid.UUID(workspace), Start: 1, Cold: true, GPUs: int(params.GpuCount),
			GPUModels: gpuModels(spec), Pinned: pinned(spec),
		})
		if err != nil {
			return fmt.Errorf("admit instance: %w", err)
		}
		if grant.Start == 0 {
			return &ConflictError{Reason: "the account's plan allows no more containers now"}
		}
		inserted, err := q.InsertInstance(ctx, params)
		if err != nil {
			return fmt.Errorf("insert instance: %w", err)
		}
		id = inserted.ID
		// Placement listens on the execution channel.
		return database.Notify(ctx, tx, database.ChannelExecution, release.String())
	})
	if err != nil {
		return Instance{}, fmt.Errorf("create instance: %w", err)
	}
	return e.Instance(ctx, workspace, ContainerID(id))
}

func instanceParams(workspace identity.WorkspaceID, row InstanceReleaseRow, spec apitypes.FunctionSpec, req InstanceRequest) (InsertInstanceParams, error) {
	kind := apitypes.WorkloadKind(row.Kind)
	pod := kind == apitypes.WorkloadKindPod || kind == apitypes.WorkloadKindSandbox
	if !req.Shell && !pod {
		return InsertInstanceParams{}, &InvalidError{Reason: fmt.Sprintf("a %s runs no instances; start a shell instead", kind)}
	}
	if len(req.Command) > 0 && (req.Shell || !pod) {
		return InsertInstanceParams{}, &InvalidError{Reason: "only a pod or sandbox instance takes a command"}
	}
	if req.Shell && req.Snapshot != nil {
		return InsertInstanceParams{}, &InvalidError{Reason: "a shell container does not restore a snapshot"}
	}
	if spec.Pod != nil && spec.Pod.Kind == apitypes.PodKindDevbox {
		return InsertInstanceParams{}, &InvalidError{Reason: "a devbox runs one container; connect to it instead"}
	}
	params := InsertInstanceParams{
		WorkspaceID: uuid.UUID(workspace), ReleaseID: &row.ID,
		CpuMillis: int64(spec.Resources.CpuMillis), MemoryBytes: int64(spec.Resources.MemoryMib) << 20,
		GpuCount:  int32(gpuCount(spec.Resources)), //nolint:gosec // The schema caps gpu_count at 8.
		RateClass: string(billing.RateClassFor(pinned(spec), preemptible(spec))),
		Purpose:   string(PurposeInstance), Command: req.Command, SnapshotID: req.Snapshot,
		AllowList: []string{}, ExposedPorts: []int32{},
	}
	if req.Shell {
		params.Purpose = string(PurposeShell)
		params.KeepWarmSeconds = ptr(int32(shellKeepWarm.Seconds()))
		return params, nil
	}
	keepWarm := 600
	if spec.KeepWarmSeconds != nil {
		keepWarm = *spec.KeepWarmSeconds
	}
	if req.Timeout != nil {
		keepWarm = *req.Timeout
	}
	if keepWarm > 0 {
		params.KeepWarmSeconds = ptr(int32(keepWarm)) //nolint:gosec // The schema caps timeouts at a week.
	}
	if p := spec.Pod; p != nil {
		params.BlockNetwork = p.BlockNetwork != nil && *p.BlockNetwork
		if p.AllowList != nil {
			params.AllowList = *p.AllowList
		}
	}
	return params, nil
}

// Instance reads a container of the workspace as an instance.
func (e *Execution) Instance(ctx context.Context, workspace identity.WorkspaceID, id ContainerID) (Instance, error) {
	row, err := e.queries.InstanceView(ctx, InstanceViewParams{ID: uuid.UUID(id), WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return Instance{}, ErrNotFound
	}
	if err != nil {
		return Instance{}, fmt.Errorf("read instance: %w", err)
	}
	out := Instance{
		ID: ContainerID(row.ID), Release: row.ReleaseID, App: row.AppName, Name: row.WorkloadName,
		Kind: apitypes.WorkloadKind(row.Kind), State: ContainerState(row.State), Purpose: ContainerPurpose(row.Purpose),
		KeepWarm: intOf(row.KeepWarmSeconds), ActiveUntil: row.ActiveUntil, ExitMessage: row.ExitMessage,
		ExitCode: intOf(row.ExitCode), CreatedAt: row.CreatedAt, ReadyAt: row.ReadyAt, Host: row.HostID,
	}
	if row.StopReason != nil {
		reason := StopReason(*row.StopReason)
		out.StopReason = &reason
	}
	if err := json.Unmarshal(row.Spec, &out.Spec); err != nil {
		return Instance{}, fmt.Errorf("decode release spec: %w", err)
	}
	out.Ports = exposedPorts(out.Spec, row.ExposedPorts)
	return out, nil
}

// exposedPorts are the release's ports, then the ones exposed since, once
// each.
func exposedPorts(spec apitypes.FunctionSpec, extra []int32) []int {
	var ports []int
	seen := map[int]bool{}
	add := func(p int) {
		if !seen[p] {
			seen[p] = true
			ports = append(ports, p)
		}
	}
	for _, p := range PodPorts(spec) {
		add(p)
	}
	for _, p := range extra {
		add(int(p))
	}
	return ports
}

// PodPorts are the ports a pod or sandbox release declares, by name order.
func PodPorts(spec apitypes.FunctionSpec) []int {
	if spec.Pod == nil || spec.Pod.Ports == nil {
		return nil
	}
	names := make([]string, 0, len(*spec.Pod.Ports))
	for name := range *spec.Pod.Ports {
		names = append(names, name)
	}
	slices.Sort(names)
	ports := make([]int, 0, len(names))
	for _, name := range names {
		ports = append(ports, (*spec.Pod.Ports)[name])
	}
	return ports
}

func intOf(v *int32) *int {
	if v == nil {
		return nil
	}
	n := int(*v)
	return &n
}

func gpuCount(r apitypes.Resources) int {
	if r.GpuCount != nil {
		return *r.GpuCount
	}
	if r.Gpu != nil && len(*r.Gpu) > 0 {
		return 1
	}
	return 0
}

func gpuModels(spec apitypes.FunctionSpec) []billing.GPUType {
	if spec.Resources.Gpu == nil {
		return nil
	}
	out := make([]billing.GPUType, len(*spec.Resources.Gpu))
	for n, g := range *spec.Resources.Gpu {
		out[n] = billing.GPUType(g)
	}
	return out
}

func pinned(spec apitypes.FunctionSpec) bool {
	p := spec.Placement
	return p != nil && ((p.Region != nil && *p.Region != "") || (p.AvailabilityZone != nil && *p.AvailabilityZone != ""))
}

func preemptible(spec apitypes.FunctionSpec) bool {
	p := spec.Placement
	return p == nil || p.Preemptible == nil || *p.Preemptible
}

// HoldContainers keeps containers active for holder: open connections
// renew this well within LeaseTTL.
func (e *Execution) HoldContainers(ctx context.Context, holder uuid.UUID, containers []ContainerID) error {
	if len(containers) == 0 {
		return nil
	}
	if err := e.queries.UpsertContainerLeases(ctx, UpsertContainerLeasesParams{
		ContainerIds: containerUUIDs(containers), HolderID: holder, TtlSeconds: LeaseTTL.Seconds(),
	}); err != nil {
		return fmt.Errorf("hold containers: %w", err)
	}
	return nil
}

// ReleaseContainers ends holder's connections to containers. Each stays
// active for its keep-warm window from now.
func (e *Execution) ReleaseContainers(ctx context.Context, holder uuid.UUID, containers []ContainerID) error {
	if len(containers) == 0 {
		return nil
	}
	if err := e.queries.EndContainerLeases(ctx, EndContainerLeasesParams{
		HolderID: holder, ContainerIds: containerUUIDs(containers),
	}); err != nil {
		return fmt.Errorf("release containers: %w", err)
	}
	return nil
}

// Touch records a call on the container as activity.
func (e *Execution) Touch(ctx context.Context, container ContainerID) error {
	if err := e.queries.TouchContainer(ctx, uuid.UUID(container)); err != nil {
		return fmt.Errorf("touch container: %w", err)
	}
	return nil
}

func containerUUIDs(ids []ContainerID) []uuid.UUID {
	out := make([]uuid.UUID, len(ids))
	for n, id := range ids {
		out[n] = uuid.UUID(id)
	}
	return out
}

// TTL is an instance's idle lifetime after a change.
type TTL struct {
	Seconds   int
	ExpiresAt *time.Time
}

// SetTTL makes ttl seconds from now the instance's idle window; 0 or -1
// keeps it up until it is stopped.
func (e *Execution) SetTTL(ctx context.Context, workspace identity.WorkspaceID, container ContainerID, ttl int) (TTL, error) {
	var keepWarm *int32
	if ttl > 0 {
		keepWarm = ptr(int32(ttl)) //nolint:gosec // The schema caps ttl at a week.
	}
	row, err := e.queries.SetContainerTTL(ctx, SetContainerTTLParams{
		KeepWarmSeconds: keepWarm, ID: uuid.UUID(container), WorkspaceID: uuid.UUID(workspace),
	})
	if errors.Is(err, pgx.ErrNoRows) {
		if _, getErr := e.Instance(ctx, workspace, container); getErr != nil {
			return TTL{}, getErr
		}
		return TTL{}, &ConflictError{Reason: "the container has stopped"}
	}
	if err != nil {
		return TTL{}, fmt.Errorf("set ttl: %w", err)
	}
	out := TTL{Seconds: -1, ExpiresAt: row.ActiveUntil}
	if row.KeepWarmSeconds != nil {
		out.Seconds = int(*row.KeepWarmSeconds)
	}
	return out, nil
}

// NetworkPolicy is a container's outbound traffic limit.
type NetworkPolicy struct {
	Block bool
	Allow []string
}

// MaxAllowList bounds a network policy's CIDR ranges.
const MaxAllowList = 10

// ParseNetworkPolicy validates and normalizes an allow list.
func ParseNetworkPolicy(block bool, allow []string) (NetworkPolicy, error) {
	if len(allow) > MaxAllowList {
		return NetworkPolicy{}, &InvalidError{Reason: fmt.Sprintf("allow_list takes at most %d CIDR ranges", MaxAllowList)}
	}
	out := NetworkPolicy{Block: block, Allow: make([]string, 0, len(allow))}
	for _, entry := range allow {
		prefix, err := netip.ParsePrefix(entry)
		if err != nil {
			addr, addrErr := netip.ParseAddr(entry)
			if addrErr != nil {
				return NetworkPolicy{}, &InvalidError{Reason: fmt.Sprintf("allow_list entry %q is not an IP address or CIDR range", entry)}
			}
			prefix = netip.PrefixFrom(addr, addr.BitLen())
		}
		out.Allow = append(out.Allow, prefix.Masked().String())
	}
	return out, nil
}

// SetNetwork replaces a live container's outbound policy and wakes its
// host, which applies it.
func (e *Execution) SetNetwork(ctx context.Context, workspace identity.WorkspaceID, container ContainerID, policy NetworkPolicy) (NetworkPolicy, error) {
	var out NetworkPolicy
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		row, err := e.queries.WithTx(tx).SetContainerNetwork(ctx, SetContainerNetworkParams{
			BlockNetwork: policy.Block, AllowList: policy.Allow, ID: uuid.UUID(container), WorkspaceID: uuid.UUID(workspace),
		})
		if errors.Is(err, pgx.ErrNoRows) {
			return &ConflictError{Reason: "the container has stopped or does not exist"}
		}
		if err != nil {
			return fmt.Errorf("set network: %w", err)
		}
		out = NetworkPolicy{Block: row.BlockNetwork, Allow: row.AllowList}
		if row.HostID == nil {
			return nil
		}
		return database.Notify(ctx, tx, database.ChannelHost, row.HostID.String())
	})
	return out, err
}

// Network reads a container's outbound policy.
func (e *Execution) Network(ctx context.Context, workspace identity.WorkspaceID, container ContainerID) (NetworkPolicy, error) {
	row, err := e.queries.ContainerNetwork(ctx, ContainerNetworkParams{ID: uuid.UUID(container), WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return NetworkPolicy{}, ErrNotFound
	}
	if err != nil {
		return NetworkPolicy{}, fmt.Errorf("read network: %w", err)
	}
	return NetworkPolicy{Block: row.BlockNetwork, Allow: row.AllowList}, nil
}

// ExposePort makes a live container answer HTTP on port through the edge.
func (e *Execution) ExposePort(ctx context.Context, workspace identity.WorkspaceID, container ContainerID, port int) error {
	_, err := e.queries.ExposePort(ctx, ExposePortParams{Port: int32(port), ID: uuid.UUID(container), WorkspaceID: uuid.UUID(workspace)}) //nolint:gosec // The schema bounds ports.
	if errors.Is(err, pgx.ErrNoRows) {
		return &ConflictError{Reason: "the container has stopped or does not exist"}
	}
	if err != nil {
		return fmt.Errorf("expose port: %w", err)
	}
	return nil
}

// ContainerRoute is what reaching a live container needs.
type ContainerRoute struct {
	ID        ContainerID
	Host      *uuid.UUID
	State     ContainerState
	Purpose   ContainerPurpose
	Workspace identity.WorkspaceID
	Workload  uuid.UUID
	Kind      apitypes.WorkloadKind
	// Accepting is false once the workload is stopped or its app paused.
	Accepting bool
	Ports     []int
	Spec      apitypes.FunctionSpec
}

// Route reads what reaching a container needs, in any workspace; callers
// authorize the workspace.
func (e *Execution) Route(ctx context.Context, container ContainerID) (ContainerRoute, error) {
	row, err := e.queries.ContainerRoute(ctx, uuid.UUID(container))
	if errors.Is(err, pgx.ErrNoRows) {
		return ContainerRoute{}, ErrNotFound
	}
	if err != nil {
		return ContainerRoute{}, fmt.Errorf("read container route: %w", err)
	}
	out := ContainerRoute{
		ID: ContainerID(row.ID), Host: row.HostID, State: ContainerState(row.State), Purpose: ContainerPurpose(row.Purpose),
		Workspace: identity.WorkspaceID(row.WorkspaceID), Workload: row.WorkloadID, Kind: apitypes.WorkloadKind(row.Kind),
		Accepting: row.AppState == "active" && row.DesiredState == "active",
	}
	if err := json.Unmarshal(row.Spec, &out.Spec); err != nil {
		return ContainerRoute{}, fmt.Errorf("decode release spec: %w", err)
	}
	out.Ports = exposedPorts(out.Spec, row.ExposedPorts)
	return out, nil
}

// StopIdle drains idle instances and shell containers, and forgets leases
// no window needs any more. Hosts stop the drained containers.
func (e *Execution) StopIdle(ctx context.Context, logger *slog.Logger) (int, error) {
	var drained []drainedContainer
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		rows, err := e.queries.WithTx(tx).DrainIdleInstances(ctx, idleBatch)
		if err != nil {
			return fmt.Errorf("drain idle instances: %w", err)
		}
		drained = drained[:0]
		for _, row := range rows {
			drained = append(drained, drainedContainer{id: row.ID, host: row.HostID})
		}
		return notifyHosts(ctx, tx, drained)
	})
	if err != nil {
		return 0, err
	}
	for _, c := range drained {
		logger.InfoContext(ctx, "idle instance draining", "container_id", c.id, "host_id", c.host)
	}
	if err := e.queries.DeleteOldContainerLeases(ctx); err != nil {
		return len(drained), fmt.Errorf("delete old leases: %w", err)
	}
	return len(drained), nil
}
