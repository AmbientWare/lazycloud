package api

import (
	"context"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// Handlers for apps, workloads, task listing, containers and logs.

func limitOf(l *apitypes.Limit) int {
	if l == nil {
		return 0
	}
	return *l
}

func cursorOf(c *apitypes.Cursor) string {
	if c == nil {
		return ""
	}
	return *c
}

func nextCursor(next string) *string {
	if next == "" {
		return nil
	}
	return &next
}

// ListApps lists apps with a deployed workload.
func (s *Server) ListApps(ctx context.Context, req ListAppsRequestObject) (ListAppsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	var state *control.AppState
	if req.Params.State != nil {
		st := control.AppState(*req.Params.State)
		state = &st
	}
	page, err := s.owners.Control.ListApps(ctx, ws.ID, control.AppFilter{State: state, Search: req.Params.Search},
		limitOf(req.Params.Limit), cursorOf(req.Params.Cursor))
	if err != nil {
		return nil, err
	}
	return ListApps200JSONResponse{Apps: page.Apps, NextCursor: nextCursor(page.Next)}, nil
}

// GetApp reads an app by name or id.
func (s *Server) GetApp(ctx context.Context, req GetAppRequestObject) (GetAppResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	app, err := s.owners.Control.GetApp(ctx, ws.ID, req.App)
	if err != nil {
		return nil, err
	}
	return GetApp200JSONResponse(app), nil
}

// PauseApp pauses an app.
func (s *Server) PauseApp(ctx context.Context, req PauseAppRequestObject) (PauseAppResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	app, err := s.owners.Control.PauseApp(ctx, ws.ID, req.App)
	if err != nil {
		return nil, err
	}
	return PauseApp200JSONResponse(app), nil
}

// ResumeApp resumes a paused app.
func (s *Server) ResumeApp(ctx context.Context, req ResumeAppRequestObject) (ResumeAppResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	app, err := s.owners.Control.ResumeApp(ctx, ws.ID, req.App)
	if err != nil {
		return nil, err
	}
	return ResumeApp200JSONResponse(app), nil
}

// DeleteApp deletes an app.
func (s *Server) DeleteApp(ctx context.Context, req DeleteAppRequestObject) (DeleteAppResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	app, err := s.owners.Control.DeleteApp(ctx, ws.ID, req.App)
	if err != nil {
		return nil, err
	}
	return DeleteApp200JSONResponse(app), nil
}

// PlanDeployment previews a deploy.
func (s *Server) PlanDeployment(ctx context.Context, req PlanDeploymentRequestObject) (PlanDeploymentResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	plan, err := s.owners.Control.PlanDeployment(ctx, ws.ID, req.App, *req.Body)
	if err != nil {
		return nil, err
	}
	return PlanDeployment200JSONResponse(plan), nil
}

// PrepareRelease returns a release for working-tree calls.
func (s *Server) PrepareRelease(ctx context.Context, req PrepareReleaseRequestObject) (PrepareReleaseResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	// A working-tree release runs its image by the same pinned reference
	// as a deployed one.
	if err := s.pinImage(ctx, ws.ID, req.Body); err != nil {
		return nil, err
	}
	release, err := s.owners.Control.PrepareRelease(ctx, ws.ID, req.App, *req.Body)
	if err != nil {
		return nil, err
	}
	return PrepareRelease200JSONResponse(release), nil
}

// ListWorkloads lists deployed workloads.
func (s *Server) ListWorkloads(ctx context.Context, req ListWorkloadsRequestObject) (ListWorkloadsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	p := req.Params
	page, err := s.owners.Control.ListWorkloads(ctx, ws.ID, control.WorkloadFilter{App: p.App, Kind: p.Kind, Name: p.Name, ID: p.Id, Search: p.Search},
		limitOf(p.Limit), cursorOf(p.Cursor))
	if err != nil {
		return nil, err
	}
	for n := range page.Workloads {
		if err := s.podWorkload(ctx, &page.Workloads[n]); err != nil {
			return nil, err
		}
	}
	return ListWorkloads200JSONResponse{Workloads: page.Workloads, NextCursor: nextCursor(page.Next)}, nil
}

// findWorkload authorizes the workspace and resolves the workload a path
// addresses by app, kind and name.
func (s *Server) findWorkload(ctx context.Context, workspace, app string, kind apitypes.WorkloadKind, name string) (identity.Workspace, control.WorkloadID, error) {
	ws, err := s.workspace(ctx, workspace)
	if err != nil {
		return identity.Workspace{}, control.WorkloadID{}, err
	}
	id, err := s.owners.Control.FindWorkload(ctx, ws.ID, control.WorkloadRef{App: app, Kind: kind, Name: name})
	return ws, id, err
}

// workloadOut reads a workload with a pod's role, count and URL.
func (s *Server) workloadOut(ctx context.Context, ws identity.Workspace, id control.WorkloadID) (apitypes.Workload, error) {
	w, err := s.owners.Control.GetWorkload(ctx, ws.ID, id)
	if err != nil {
		return apitypes.Workload{}, err
	}
	return w, s.podWorkload(ctx, &w)
}

// GetWorkload describes a workload: the release its active version, or the
// named one, runs, where an HTTP workload answers and a function's schedule.
func (s *Server) GetWorkload(ctx context.Context, req GetWorkloadRequestObject) (GetWorkloadResponseObject, error) {
	ws, id, err := s.findWorkload(ctx, req.Workspace, req.App, req.Kind, req.Name)
	if err != nil {
		return nil, err
	}
	w, err := s.workloadOut(ctx, ws, id)
	if err != nil {
		return nil, err
	}
	release, err := s.owners.Control.Release(ctx, ws.ID, id, req.Params.Version)
	if err != nil {
		return nil, err
	}
	out := apitypes.WorkloadDetail{Workload: w, Release: release}
	switch req.Kind {
	case apitypes.WorkloadKindEndpoint, apitypes.WorkloadKindAsgi:
		urls, err := s.owners.Edge.HTTPUrls(ctx, ws.Name, req.App, uuid.UUID(id), release)
		if err != nil {
			return nil, err //nolint:wrapcheck // the edge's typed errors map to responses
		}
		out.Http = &urls
		out.Release.Url, out.Release.InvokePath = &urls.Url, &urls.InvokePath
	case apitypes.WorkloadKindFunction:
		schedule, err := s.owners.Schedules.ForFunction(ctx, ws.ID, req.App, req.Name)
		if err != nil {
			return nil, err
		}
		if schedule != nil {
			sched := scheduleOut(*schedule)
			out.Schedule = &sched
		}
	case apitypes.WorkloadKindPod, apitypes.WorkloadKindSandbox:
		out.Release.Url = w.Url
	}
	return GetWorkload200JSONResponse(out), nil
}

// StopWorkload stops a workload.
func (s *Server) StopWorkload(ctx context.Context, req StopWorkloadRequestObject) (StopWorkloadResponseObject, error) {
	ws, id, err := s.findWorkload(ctx, req.Workspace, req.App, req.Kind, req.Name)
	if err != nil {
		return nil, err
	}
	w, err := s.owners.Control.StopWorkload(ctx, ws.ID, id)
	if err != nil {
		return nil, err
	}
	return StopWorkload200JSONResponse(w), nil
}

// StartWorkload starts a workload, optionally on another version.
func (s *Server) StartWorkload(ctx context.Context, req StartWorkloadRequestObject) (StartWorkloadResponseObject, error) {
	ws, id, err := s.findWorkload(ctx, req.Workspace, req.App, req.Kind, req.Name)
	if err != nil {
		return nil, err
	}
	var version *int
	if req.Body != nil {
		version = req.Body.Version
	}
	w, err := s.owners.Control.StartWorkload(ctx, ws.ID, id, version)
	if err != nil {
		return nil, err
	}
	return StartWorkload200JSONResponse(w), nil
}

// DeleteWorkload deletes a workload.
func (s *Server) DeleteWorkload(ctx context.Context, req DeleteWorkloadRequestObject) (DeleteWorkloadResponseObject, error) {
	ws, id, err := s.findWorkload(ctx, req.Workspace, req.App, req.Kind, req.Name)
	if err != nil {
		return nil, err
	}
	w, err := s.owners.Control.DeleteWorkload(ctx, ws.ID, id)
	if err != nil {
		return nil, err
	}
	return DeleteWorkload200JSONResponse(w), nil
}

// ListWorkloadVersions lists a workload's deployed versions.
func (s *Server) ListWorkloadVersions(ctx context.Context, req ListWorkloadVersionsRequestObject) (ListWorkloadVersionsResponseObject, error) {
	ws, id, err := s.findWorkload(ctx, req.Workspace, req.App, req.Kind, req.Name)
	if err != nil {
		return nil, err
	}
	page, err := s.owners.Control.ListVersions(ctx, ws.ID, id, limitOf(req.Params.Limit), cursorOf(req.Params.Cursor))
	if err != nil {
		return nil, err
	}
	return ListWorkloadVersions200JSONResponse{Versions: page.Versions, NextCursor: nextCursor(page.Next)}, nil
}

// StreamWorkloadLogs writes the workload's log entries as NDJSON.
func (s *Server) StreamWorkloadLogs(ctx context.Context, req StreamWorkloadLogsRequestObject) (StreamWorkloadLogsResponseObject, error) {
	ws, id, err := s.findWorkload(ctx, req.Workspace, req.App, req.Kind, req.Name)
	if err != nil {
		return nil, err
	}
	return s.logStream(ctx, ws, execution.LogSource{Kind: execution.LogsOfWorkload, ID: uuid.UUID(id)},
		req.Params.After, req.Params.Tail, req.Params.Follow)
}

// ListWorkloadContainers lists the workload's containers newest first.
func (s *Server) ListWorkloadContainers(ctx context.Context, req ListWorkloadContainersRequestObject) (ListWorkloadContainersResponseObject, error) {
	ws, id, err := s.findWorkload(ctx, req.Workspace, req.App, req.Kind, req.Name)
	if err != nil {
		return nil, err
	}
	filter := execution.ContainerFilter{Live: req.Params.Live != nil && *req.Params.Live, Workload: (*uuid.UUID)(&id)}
	page, err := s.containerPage(ctx, ws, filter, req.Params.Limit, req.Params.Cursor)
	if err != nil {
		return nil, err
	}
	return ListWorkloadContainers200JSONResponse(page), nil
}

// ListTasks lists tasks newest first.
func (s *Server) ListTasks(ctx context.Context, req ListTasksRequestObject) (ListTasksResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	filter := execution.TaskFilter{
		App: req.Params.App, Function: req.Params.Function, Search: req.Params.Search, Version: req.Params.Version,
		RootOnly: req.Params.RootOnly != nil && *req.Params.RootOnly,
	}
	if req.Params.Status != nil {
		st := execution.TaskStatus(*req.Params.Status)
		filter.Status = &st
	}
	page, err := s.owners.Execution.ListTasks(ctx, ws.ID, filter, limitOf(req.Params.Limit), cursorOf(req.Params.Cursor))
	if err != nil {
		return nil, err
	}
	out := ListTasks200JSONResponse{Tasks: make([]apitypes.Task, len(page.Tasks)), NextCursor: nextCursor(page.Next)}
	for n, task := range page.Tasks {
		out.Tasks[n] = taskOut(task)
	}
	return out, nil
}

// StopTasks cancels each listed queued or running task.
func (s *Server) StopTasks(ctx context.Context, req StopTasksRequestObject) (StopTasksResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	ids := make([]execution.TaskID, len(req.Body.TaskIds))
	for n, id := range req.Body.TaskIds {
		ids[n] = execution.TaskID(id)
	}
	result, err := s.owners.Execution.StopTasks(ctx, ws.ID, ids)
	if err != nil {
		return nil, err
	}
	return StopTasks200JSONResponse{Stopped: taskUUIDs(result.Stopped), Skipped: taskUUIDs(result.Skipped)}, nil
}

func taskUUIDs(ids []execution.TaskID) []uuid.UUID {
	out := make([]uuid.UUID, len(ids))
	for n, id := range ids {
		out[n] = uuid.UUID(id)
	}
	return out
}

// RerunTask submits a task's input again.
func (s *Server) RerunTask(ctx context.Context, req RerunTaskRequestObject) (RerunTaskResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	task, err := s.owners.Execution.RerunTask(ctx, ws.ID, execution.TaskID(req.Task))
	if err != nil {
		return nil, err
	}
	return RerunTask201JSONResponse(taskOut(task)), nil
}

// ListContainers lists containers newest first.
func (s *Server) ListContainers(ctx context.Context, req ListContainersRequestObject) (ListContainersResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	filter := execution.ContainerFilter{Live: req.Params.Live != nil && *req.Params.Live, App: req.Params.App}
	page, err := s.containerPage(ctx, ws, filter, req.Params.Limit, req.Params.Cursor)
	if err != nil {
		return nil, err
	}
	return ListContainers200JSONResponse(page), nil
}

func (s *Server) containerPage(ctx context.Context, ws identity.Workspace, filter execution.ContainerFilter, limit *apitypes.PageLimit, cursor *apitypes.Cursor) (apitypes.ContainerPage, error) {
	page, err := s.owners.Execution.ListContainers(ctx, ws.ID, filter, limitOf(limit), cursorOf(cursor))
	if err != nil {
		return apitypes.ContainerPage{}, err
	}
	out := apitypes.ContainerPage{Containers: make([]apitypes.Container, len(page.Containers)), NextCursor: nextCursor(page.Next)}
	for n, c := range page.Containers {
		out.Containers[n] = containerOut(c)
	}
	return out, nil
}

// GetContainer reads a container.
func (s *Server) GetContainer(ctx context.Context, req GetContainerRequestObject) (GetContainerResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	c, err := s.owners.Execution.GetContainer(ctx, ws.ID, execution.ContainerID(req.Container))
	if err != nil {
		return nil, err
	}
	return GetContainer200JSONResponse(containerOut(c)), nil
}

// StopContainer stops a container now.
func (s *Server) StopContainer(ctx context.Context, req StopContainerRequestObject) (StopContainerResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if err := s.ownsContainer(ctx, execution.ContainerID(req.Container)); err != nil {
		return nil, err
	}
	c, err := s.owners.Execution.StopContainer(ctx, ws.ID, execution.ContainerID(req.Container))
	if err != nil {
		return nil, err
	}
	return StopContainer200JSONResponse(containerOut(c)), nil
}

// StreamContainerLogs writes the container's log entries as NDJSON.
func (s *Server) StreamContainerLogs(ctx context.Context, req StreamContainerLogsRequestObject) (StreamContainerLogsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	return s.logStream(ctx, ws, execution.LogSource{Kind: execution.LogsOfContainer, ID: req.Container},
		req.Params.After, req.Params.Tail, req.Params.Follow)
}

func containerOut(c execution.Container) apitypes.Container {
	out := apitypes.Container{
		Id: uuid.UUID(c.ID), App: c.App, Function: c.Function, ReleaseId: c.Release, Version: c.Version,
		State: apitypes.ContainerState(c.State), ExitMessage: c.ExitMessage, Slots: c.Slots, RunningTasks: c.RunningTasks,
		CpuMillis: c.CPUMillis, MemoryMib: c.MemoryBytes >> 20,
		CreatedAt: c.CreatedAt, ReadyAt: c.ReadyAt, StoppedAt: c.StoppedAt,
		Kind: &c.Kind, ExitCode: c.ExitCode, Host: c.Host, GpuCount: &c.GPUCount, ExpiresAt: c.ExpiresAt,
	}
	purpose := apitypes.ContainerPurpose(c.Purpose)
	out.Purpose = &purpose
	if c.StopReason != nil {
		r := apitypes.StopReason(*c.StopReason)
		out.StopReason = &r
	}
	if c.Image != "" {
		out.Image = &c.Image
	}
	return out
}
