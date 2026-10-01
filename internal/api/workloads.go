package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/edge"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

// Pods, devboxes, sandboxes, instances and SSH. Container operations that
// reach the supervisor are in container_proxy.go.

// podDeployment adds a pod's role, count and URL to its deployment.
func (s *Server) podDeployment(ctx context.Context, d *apitypes.DeployedWorkload) error {
	if d.Kind != apitypes.WorkloadKindPod || d.ReleaseId == nil {
		return nil
	}
	release, err := s.owners.Execution.PodRelease(ctx, *d.ReleaseId)
	if err != nil {
		return err
	}
	spec := release.Spec
	role := apitypes.PodRolePod
	if spec.Pod != nil && spec.Pod.Kind == apitypes.PodKindDevbox {
		role = apitypes.PodRoleDevbox
	}
	d.Role = &role
	if url := s.owners.Edge.PodURL(*d.ReleaseId, spec); url != "" {
		d.Url = &url
	}
	replicas, err := s.owners.Execution.PodReplicas(ctx, d.Id)
	if err != nil {
		return err
	}
	scaling := apitypes.Scaling{MaxContainers: 1}
	if spec.Autoscaler != nil && spec.Autoscaler.MaxContainers != nil {
		scaling.MaxContainers = *spec.Autoscaler.MaxContainers
	}
	if spec.KeepWarmSeconds != nil && *spec.KeepWarmSeconds == -1 {
		scaling.MinContainers = 1
	}
	if replicas != nil {
		scaling = apitypes.Scaling{MinContainers: *replicas, MaxContainers: *replicas}
	}
	d.Scaling = &scaling
	return nil
}

// ScaleDeployment holds a pod at a number of containers.
func (s *Server) ScaleDeployment(ctx context.Context, req ScaleDeploymentRequestObject) (ScaleDeploymentResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Execution.ScalePod(ctx, ws.ID, req.Deployment, req.Body.Containers); err != nil {
		return nil, err
	}
	d, err := s.owners.Control.GetDeployment(ctx, ws.ID, control.WorkloadID(req.Deployment))
	if err != nil {
		return nil, err
	}
	if err := s.podDeployment(ctx, &d); err != nil {
		return nil, err
	}
	return ScaleDeployment200JSONResponse(d), nil
}

func (s *Server) devbox(ctx context.Context, ws identity.Workspace, deployment uuid.UUID) (apitypes.Devbox, error) {
	var disk *apitypes.Disk
	saving := func(spec apitypes.FunctionSpec) (bool, error) {
		d, err := s.rootDisk(ctx, ws.ID, spec)
		disk = d
		return d != nil && d.Status == apitypes.Saving, err
	}
	view, err := s.owners.Execution.PodView(ctx, ws.ID, deployment, saving)
	if err != nil {
		return apitypes.Devbox{}, err
	}
	if view.Spec.Pod == nil || view.Spec.Pod.Kind != apitypes.PodKindDevbox {
		return apitypes.Devbox{}, execution.ErrNotFound
	}
	if disk == nil {
		if disk, err = s.rootDisk(ctx, ws.ID, view.Spec); err != nil {
			return apitypes.Devbox{}, err
		}
	}
	command := "lazycloud devbox " + view.Name + " ssh"
	if view.Ambiguous {
		command += " --app " + view.App
	}
	out := apitypes.Devbox{
		DeploymentId: view.Workload, Name: view.Name, App: view.App, SshCommand: command,
		SshHost: execution.SSHAlias(ws.Name, view.App, view.Name), Phase: view.Phase,
		OpenConnections: view.Connections, IdleDeadline: view.IdleDeadline, Resources: &view.Spec.Resources,
		ContainerId: (*uuid.UUID)(view.Container), FailedContainerId: (*uuid.UUID)(view.Failed),
	}
	if view.Reason != "" {
		out.PhaseReason = &view.Reason
	}
	switch view.Phase {
	case apitypes.DevboxPhaseRunning:
		out.State = apitypes.DevboxStateRunning
	case apitypes.DevboxPhaseQueued, apitypes.DevboxPhasePullingImage, apitypes.DevboxPhaseRestoringDisk, apitypes.DevboxPhaseStarting:
		out.State = apitypes.DevboxStateStarting
	case apitypes.DevboxPhaseStopped, apitypes.DevboxPhaseStopping, apitypes.DevboxPhaseFailed:
		out.State = apitypes.DevboxStateStopped
	}
	if disk != nil {
		out.Disk = &apitypes.DevboxDisk{
			Name: disk.Name, SizeBytes: disk.SizeBytes, StoredBytes: disk.StoredBytes, Generation: disk.Generation, Status: disk.Status,
		}
	}
	return out, nil
}

// rootDisk is a devbox's root disk, nil before its first start.
func (s *Server) rootDisk(ctx context.Context, ws identity.WorkspaceID, spec apitypes.FunctionSpec) (*apitypes.Disk, error) {
	if spec.Disks == nil {
		return nil, nil //nolint:nilnil // A pod without disks has no root disk.
	}
	for _, d := range *spec.Disks {
		if d.MountPath != "/" {
			continue
		}
		disk, err := s.owners.Storage.GetDisk(ctx, ws, d.Name)
		if errors.Is(err, storage.ErrNotFound) {
			return nil, nil //nolint:nilnil // The disk is created on first start.
		}
		if err != nil {
			return nil, err
		}
		return &disk, nil
	}
	return nil, nil //nolint:nilnil // A pod without a root disk.
}

// GetDevbox reads a devbox.
func (s *Server) GetDevbox(ctx context.Context, req GetDevboxRequestObject) (GetDevboxResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	out, err := s.devbox(ctx, ws, req.Deployment)
	if err != nil {
		return nil, err
	}
	return GetDevbox200JSONResponse(out), nil
}

// StartDevbox starts a devbox now, activating a stopped deployment first.
func (s *Server) StartDevbox(ctx context.Context, req StartDevboxRequestObject) (StartDevboxResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if _, err := s.devbox(ctx, ws, req.Deployment); err != nil {
		return nil, err
	}
	d, err := s.owners.Control.GetDeployment(ctx, ws.ID, control.WorkloadID(req.Deployment))
	if err != nil {
		return nil, err
	}
	if d.State != apitypes.WorkloadStateActive {
		if _, err := s.owners.Control.StartDeployment(ctx, ws.ID, control.WorkloadID(req.Deployment), nil); err != nil {
			return nil, err
		}
	}
	if err := s.owners.Execution.WakePod(ctx, ws.ID, req.Deployment); err != nil {
		return nil, err
	}
	out, err := s.devbox(ctx, ws, req.Deployment)
	if err != nil {
		return nil, err
	}
	return StartDevbox200JSONResponse(out), nil
}

// StopDevbox stops a devbox until its next connection or start.
func (s *Server) StopDevbox(ctx context.Context, req StopDevboxRequestObject) (StopDevboxResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if _, err := s.devbox(ctx, ws, req.Deployment); err != nil {
		return nil, err
	}
	if err := s.owners.Execution.ParkPod(ctx, ws.ID, req.Deployment); err != nil {
		return nil, err
	}
	out, err := s.devbox(ctx, ws, req.Deployment)
	if err != nil {
		return nil, err
	}
	return StopDevbox200JSONResponse(out), nil
}

func (s *Server) instanceOut(i execution.Instance) apitypes.Instance {
	out := apitypes.Instance{
		Id: uuid.UUID(i.ID), ReleaseId: i.Release, App: i.App, Name: i.Name, Kind: i.Kind,
		State: apitypes.ContainerState(i.State), ExitMessage: i.ExitMessage, ExitCode: i.ExitCode,
		CreatedAt: i.CreatedAt, ReadyAt: i.ReadyAt,
	}
	timeout := -1
	if i.KeepWarm != nil {
		timeout = *i.KeepWarm
		out.ExpiresAt = i.ActiveUntil
	}
	out.TimeoutSeconds = &timeout
	if len(i.Ports) > 0 {
		url := s.owners.Edge.ContainerPortURL(uuid.UUID(i.ID), i.Ports[0])
		out.Url = &url
	}
	if i.StopReason != nil {
		r := apitypes.StopReason(*i.StopReason)
		out.StopReason = &r
	}
	return out
}

// CreateInstance starts an instance, shell container or restored sandbox.
func (s *Server) CreateInstance(ctx context.Context, req CreateInstanceRequestObject) (CreateInstanceResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	b := req.Body
	in := execution.InstanceRequest{Release: b.ReleaseId, Snapshot: b.SnapshotId, Timeout: b.TimeoutSeconds, Shell: b.Shell != nil && *b.Shell}
	if b.Command != nil {
		in.Command = *b.Command
	}
	i, err := s.owners.Execution.CreateInstance(ctx, ws.ID, in)
	if err != nil {
		return nil, err
	}
	return CreateInstance201JSONResponse(s.instanceOut(i)), nil
}

// errStillStarting answers a connect whose container is not ready yet.
var errStillStarting = errors.New("the container is still starting")

// ConnectContainer waits until the container is ready.
func (s *Server) ConnectContainer(ctx context.Context, req ConnectContainerRequestObject) (ConnectContainerResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	wait := 0
	if req.Params.WaitSeconds != nil {
		wait = *req.Params.WaitSeconds
	}
	deadline := time.Now().Add(time.Duration(wait) * time.Second)
	for {
		i, err := s.owners.Execution.Instance(ctx, ws.ID, execution.ContainerID(req.Container))
		if err != nil {
			return nil, err
		}
		switch i.State {
		case execution.ContainerReady:
			if err := s.owners.Execution.Touch(ctx, i.ID); err != nil {
				return nil, err
			}
			return ConnectContainer200JSONResponse(s.instanceOut(i)), nil
		case execution.ContainerStopped, execution.ContainerDraining:
			return nil, &execution.ConflictError{Reason: fmt.Sprintf("the container is %s", i.State)}
		case execution.ContainerPending, execution.ContainerStarting:
		}
		if !time.Now().Before(deadline) {
			return nil, errStillStarting
		}
		select {
		case <-ctx.Done():
			return nil, errStillStarting
		case <-time.After(min(250*time.Millisecond, time.Until(deadline))):
		}
	}
}

// ListSandboxes lists sandbox containers.
func (s *Server) ListSandboxes(ctx context.Context, req ListSandboxesRequestObject) (ListSandboxesResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	page, err := s.owners.Execution.ListSandboxes(ctx, ws.ID, req.Params.App, limitOf(req.Params.Limit), cursorOf(req.Params.Cursor))
	if err != nil {
		return nil, err
	}
	return ListSandboxes200JSONResponse{Sandboxes: page.Sandboxes, NextCursor: nextCursor(page.Next)}, nil
}

// GetSandboxStats counts sandboxes.
func (s *Server) GetSandboxStats(ctx context.Context, req GetSandboxStatsRequestObject) (GetSandboxStatsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	stats, err := s.owners.Execution.SandboxStats(ctx, ws.ID, req.Params.App)
	if err != nil {
		return nil, err
	}
	return GetSandboxStats200JSONResponse(stats), nil
}

// CreateSshCertificate signs a public key for the workspace's pods.
func (s *Server) CreateSshCertificate(ctx context.Context, req CreateSshCertificateRequestObject) (CreateSshCertificateResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	holder := "container"
	if p, ok := principalFrom(ctx); ok {
		holder = "user:" + uuid.UUID(p.User).String()
		if p.Token != nil {
			holder = "token:" + uuid.UUID(*p.Token).String()
		}
	}
	cert, err := s.owners.SSH.Sign(ctx, ws.ID, holder, strings.TrimSpace(req.Body.PublicKey))
	if err != nil {
		return nil, err
	}
	return CreateSshCertificate201JSONResponse{Certificate: cert.Line, Principal: execution.SSHPrincipal, ExpiresAt: cert.ExpiresAt}, nil
}

// ListSshHosts lists pods that serve SSH.
func (s *Server) ListSshHosts(ctx context.Context, req ListSshHostsRequestObject) (ListSshHostsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	hosts, next, err := s.owners.SSH.Hosts(ctx, ws, execution.SSHHostFilter{App: req.Params.App, Pod: req.Params.Pod, Role: req.Params.Role},
		cursorOf(req.Params.Cursor), limitOf(req.Params.Limit))
	if err != nil {
		return nil, err
	}
	out := ListSshHosts200JSONResponse{Hosts: make([]apitypes.SshHost, len(hosts)), NextCursor: nextCursor(next)}
	for n, h := range hosts {
		out.Hosts[n] = apitypes.SshHost{App: h.App, Pod: h.Pod, Role: h.Role, DeploymentId: h.Workload, Alias: h.Alias, HostPublicKey: h.PublicKey}
	}
	return out, nil
}

// SetContainerTtl changes an instance's idle lifetime.
func (s *Server) SetContainerTtl(ctx context.Context, req SetContainerTtlRequestObject) (SetContainerTtlResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	ttl, err := s.owners.Execution.SetTTL(ctx, ws.ID, execution.ContainerID(req.Container), req.Body.Ttl)
	if err != nil {
		return nil, err
	}
	return SetContainerTtl200JSONResponse{Ttl: ttl.Seconds, ExpiresAt: ttl.ExpiresAt}, nil
}

// GetContainerNetwork reads a container's outbound policy.
func (s *Server) GetContainerNetwork(ctx context.Context, req GetContainerNetworkRequestObject) (GetContainerNetworkResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	p, err := s.owners.Execution.Network(ctx, ws.ID, execution.ContainerID(req.Container))
	if err != nil {
		return nil, err
	}
	return GetContainerNetwork200JSONResponse{BlockNetwork: p.Block, AllowList: p.Allow}, nil
}

// SetContainerNetwork replaces a container's outbound policy.
func (s *Server) SetContainerNetwork(ctx context.Context, req SetContainerNetworkRequestObject) (SetContainerNetworkResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	policy, err := execution.ParseNetworkPolicy(req.Body.BlockNetwork, req.Body.AllowList)
	if err != nil {
		return nil, err
	}
	p, err := s.owners.Execution.SetNetwork(ctx, ws.ID, execution.ContainerID(req.Container), policy)
	if err != nil {
		return nil, err
	}
	return SetContainerNetwork200JSONResponse{BlockNetwork: p.Block, AllowList: p.Allow}, nil
}

// ListContainerPorts lists a container's exposed ports.
func (s *Server) ListContainerPorts(ctx context.Context, req ListContainerPortsRequestObject) (ListContainerPortsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	i, err := s.owners.Execution.Instance(ctx, ws.ID, execution.ContainerID(req.Container))
	if err != nil {
		return nil, err
	}
	out := ListContainerPorts200JSONResponse{Ports: make([]apitypes.ContainerPort, len(i.Ports))}
	for n, port := range i.Ports {
		out.Ports[n] = apitypes.ContainerPort{Port: port, Url: s.owners.Edge.ContainerPortURL(uuid.UUID(i.ID), port)}
	}
	return out, nil
}

// ExposeContainerPort makes a port answer through the edge.
func (s *Server) ExposeContainerPort(ctx context.Context, req ExposeContainerPortRequestObject) (ExposeContainerPortResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	container := execution.ContainerID(req.Container)
	if err := s.owners.Execution.ExposePort(ctx, ws.ID, container, req.Body.Port); err != nil {
		return nil, err
	}
	if err := s.owners.Execution.Touch(ctx, container); err != nil {
		return nil, err
	}
	return ExposeContainerPort200JSONResponse{Port: req.Body.Port, Url: s.owners.Edge.ContainerPortURL(req.Container, req.Body.Port)}, nil
}

// SnapshotContainer checkpoints a running container and waits for the
// snapshot.
func (s *Server) SnapshotContainer(ctx context.Context, req SnapshotContainerRequestObject) (SnapshotContainerResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	var id *uuid.UUID
	if req.Body != nil {
		id = req.Body.SnapshotId
	}
	snap, err := s.owners.Execution.CreateSnapshot(ctx, ws.ID, execution.ContainerID(req.Container), id)
	if err != nil {
		return nil, err
	}
	wait, cancel := context.WithTimeout(ctx, execution.SnapshotDeadline)
	defer cancel()
	if snap, err = s.owners.Execution.WaitSnapshot(wait, s.owners.Listener, snap.ID); err != nil {
		return nil, err
	}
	if snap.State == apitypes.MemorySnapshotStateFailed {
		return nil, execution.SnapshotFailure(snap)
	}
	return SnapshotContainer201JSONResponse{
		Id: snap.ID, ContainerId: req.Container, ReleaseId: snap.Release, State: snap.State,
		SizeBytes: snap.SizeBytes, CreatedAt: snap.CreatedAt,
	}, nil
}

// CreateFilesystemImage publishes a container's filesystem and waits for
// the image.
func (s *Server) CreateFilesystemImage(ctx context.Context, req CreateFilesystemImageRequestObject) (CreateFilesystemImageResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	id, err := s.owners.Execution.CreateFilesystemImage(ctx, ws.ID, execution.ContainerID(req.Container))
	if err != nil {
		return nil, err
	}
	wait, cancel := context.WithTimeout(ctx, execution.PublishDeadline)
	defer cancel()
	image, err := s.owners.Execution.WaitFilesystemImage(wait, s.owners.Listener, id)
	if err != nil {
		return nil, err
	}
	if image.ImageID == nil {
		reason := "publishing the filesystem failed"
		if image.Failure != nil {
			reason += ": " + *image.Failure
		}
		return nil, &execution.ConflictError{Reason: reason}
	}
	return CreateFilesystemImage201JSONResponse{ImageId: *image.ImageID}, nil
}

// workloadError maps workload errors; it reports whether it wrote one.
func workloadError(w http.ResponseWriter, err error) bool {
	var (
		invalid     *execution.InvalidError
		conflict    *execution.ConflictError
		unsupported *execution.UnsupportedError
	)
	switch {
	case errors.As(err, &invalid):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, invalid.Error())
	case errors.As(err, &conflict):
		writeJSONError(w, http.StatusConflict, apitypes.Conflict, conflict.Error())
	case errors.As(err, &unsupported):
		writeJSONError(w, http.StatusNotImplemented, apitypes.Unsupported, unsupported.Error())
	case errors.Is(err, errStillStarting):
		writeJSONError(w, http.StatusServiceUnavailable, apitypes.Unavailable, err.Error())
	case errors.Is(err, edge.ErrContainerUnreachable), errors.Is(err, errContainerNotRunning):
		writeJSONError(w, http.StatusServiceUnavailable, apitypes.Unavailable, "the container is not running")
	case errors.Is(err, execution.ErrPodStopped), errors.Is(err, execution.ErrPodWithoutSSH):
		writeJSONError(w, http.StatusConflict, apitypes.Conflict, err.Error())
	default:
		return false
	}
	return true
}
