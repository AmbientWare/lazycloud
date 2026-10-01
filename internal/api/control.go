package api

import (
	"context"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/execution"
)

// Handlers for apps, deployments, task listing, containers and logs.

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
	page, err := s.owners.Control.ListApps(ctx, ws.ID, state, limitOf(req.Params.Limit), cursorOf(req.Params.Cursor))
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

// PrepareFunctionRelease returns a release for working-tree calls.
func (s *Server) PrepareFunctionRelease(ctx context.Context, req PrepareFunctionReleaseRequestObject) (PrepareFunctionReleaseResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	// A working-tree release runs its image by the same pinned reference
	// as a deployed one.
	if err := s.pinImage(ctx, ws.ID, req.Body); err != nil {
		return nil, err
	}
	release, err := s.owners.Control.PrepareRelease(ctx, ws.ID, req.App, req.Function, *req.Body)
	if err != nil {
		return nil, err
	}
	return PrepareFunctionRelease200JSONResponse(release), nil
}

// ListDeployments lists deployed workloads.
func (s *Server) ListDeployments(ctx context.Context, req ListDeploymentsRequestObject) (ListDeploymentsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	page, err := s.owners.Control.ListDeployments(ctx, ws.ID, control.DeploymentFilter{App: req.Params.App, Name: req.Params.Name},
		limitOf(req.Params.Limit), cursorOf(req.Params.Cursor))
	if err != nil {
		return nil, err
	}
	return ListDeployments200JSONResponse{Deployments: page.Deployments, NextCursor: nextCursor(page.Next)}, nil
}

// GetDeployment reads a workload.
func (s *Server) GetDeployment(ctx context.Context, req GetDeploymentRequestObject) (GetDeploymentResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	d, err := s.owners.Control.GetDeployment(ctx, ws.ID, control.WorkloadID(req.Deployment))
	if err != nil {
		return nil, err
	}
	return GetDeployment200JSONResponse(d), nil
}

// StopDeployment stops a workload.
func (s *Server) StopDeployment(ctx context.Context, req StopDeploymentRequestObject) (StopDeploymentResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	d, err := s.owners.Control.StopDeployment(ctx, ws.ID, control.WorkloadID(req.Deployment))
	if err != nil {
		return nil, err
	}
	return StopDeployment200JSONResponse(d), nil
}

// StartDeployment starts a workload, optionally on another version.
func (s *Server) StartDeployment(ctx context.Context, req StartDeploymentRequestObject) (StartDeploymentResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	var version *int
	if req.Body != nil {
		version = req.Body.Version
	}
	d, err := s.owners.Control.StartDeployment(ctx, ws.ID, control.WorkloadID(req.Deployment), version)
	if err != nil {
		return nil, err
	}
	return StartDeployment200JSONResponse(d), nil
}

// DeleteDeployment deletes a workload.
func (s *Server) DeleteDeployment(ctx context.Context, req DeleteDeploymentRequestObject) (DeleteDeploymentResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	d, err := s.owners.Control.DeleteDeployment(ctx, ws.ID, control.WorkloadID(req.Deployment))
	if err != nil {
		return nil, err
	}
	return DeleteDeployment200JSONResponse(d), nil
}

// ListDeploymentVersions lists a workload's deployed versions.
func (s *Server) ListDeploymentVersions(ctx context.Context, req ListDeploymentVersionsRequestObject) (ListDeploymentVersionsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	page, err := s.owners.Control.ListVersions(ctx, ws.ID, control.WorkloadID(req.Deployment),
		limitOf(req.Params.Limit), cursorOf(req.Params.Cursor))
	if err != nil {
		return nil, err
	}
	return ListDeploymentVersions200JSONResponse{Versions: page.Versions, NextCursor: nextCursor(page.Next)}, nil
}

// StreamDeploymentLogs writes the workload's log entries as NDJSON.
func (s *Server) StreamDeploymentLogs(ctx context.Context, req StreamDeploymentLogsRequestObject) (StreamDeploymentLogsResponseObject, error) {
	return s.logStream(ctx, req.Workspace, execution.LogSource{Kind: execution.LogsOfWorkload, ID: req.Deployment},
		req.Params.After, req.Params.Tail, req.Params.Follow)
}

// ListTasks lists tasks newest first.
func (s *Server) ListTasks(ctx context.Context, req ListTasksRequestObject) (ListTasksResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	filter := execution.TaskFilter{App: req.Params.App, Function: req.Params.Function}
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
	live := req.Params.Live != nil && *req.Params.Live
	page, err := s.owners.Execution.ListContainers(ctx, ws.ID, live, limitOf(req.Params.Limit), cursorOf(req.Params.Cursor))
	if err != nil {
		return nil, err
	}
	out := ListContainers200JSONResponse{Containers: make([]apitypes.Container, len(page.Containers)), NextCursor: nextCursor(page.Next)}
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
	c, err := s.owners.Execution.StopContainer(ctx, ws.ID, execution.ContainerID(req.Container))
	if err != nil {
		return nil, err
	}
	return StopContainer200JSONResponse(containerOut(c)), nil
}

// StreamContainerLogs writes the container's log entries as NDJSON.
func (s *Server) StreamContainerLogs(ctx context.Context, req StreamContainerLogsRequestObject) (StreamContainerLogsResponseObject, error) {
	return s.logStream(ctx, req.Workspace, execution.LogSource{Kind: execution.LogsOfContainer, ID: req.Container},
		req.Params.After, req.Params.Tail, req.Params.Follow)
}

func containerOut(c execution.Container) apitypes.Container {
	out := apitypes.Container{
		Id: uuid.UUID(c.ID), App: c.App, Function: c.Function, ReleaseId: c.Release, Version: c.Version,
		State: apitypes.ContainerState(c.State), ExitMessage: c.ExitMessage, Slots: c.Slots, RunningTasks: c.RunningTasks,
		CpuMillis: c.CPUMillis, MemoryMib: c.MemoryBytes >> 20,
		CreatedAt: c.CreatedAt, ReadyAt: c.ReadyAt, StoppedAt: c.StoppedAt,
	}
	if c.StopReason != nil {
		r := apitypes.StopReason(*c.StopReason)
		out.StopReason = &r
	}
	return out
}
