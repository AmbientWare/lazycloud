package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

// server implements the public operations. Each invokes one owner for the
// workspace authorizeWorkspace authorized and maps its result.
type server struct {
	owners Owners
	cfg    Config
	logger *slog.Logger
}

var _ StrictServerInterface = (*server)(nil)

// refusedToContainers are the operations a container may not call: a
// function may drive the instances it started, nothing more.
var refusedToContainers = map[string]bool{
	"ScaleWorkload": true, "GetDevbox": true, "StartDevbox": true, "StopDevbox": true,
	"ListSandboxes": true, "GetSandboxStats": true, "CreateSshCertificate": true, "ListSshHosts": true,
	"SetContainerTtl": true, "GetContainerNetwork": true, "SetContainerNetwork": true,
	"SnapshotContainer": true, "CreateFilesystemImage": true, "OpenContainerShell": true, "OpenSshTunnel": true,
}

// identityScoped are the operations on a workspace whose roles identity
// checks itself, in any workspace state.
var identityScoped = map[string]bool{
	"GetWorkspace": true, "RenameWorkspace": true, "DeleteWorkspace": true,
	"ListMembers": true, "SetMemberRole": true, "RemoveMember": true, "ListInvitations": true,
	"CreateInvitation": true, "RevokeInvitation": true, "ResendInvitation": true,
}

type workspaceKey struct{}

// authorizeWorkspace refuses a container the operations refusedToContainers
// names, then authorizes the workspace the operation's path names for its
// handler, which reads it with workspaceFrom.
func (s *server) authorizeWorkspace(next StrictHandlerFunc, operationID string) StrictHandlerFunc {
	return func(ctx context.Context, w http.ResponseWriter, r *http.Request, request any) (any, error) {
		if _, ok := containerFrom(ctx); ok && refusedToContainers[operationID] {
			return nil, errFromContainer
		}
		name := r.PathValue("workspace")
		if name == "" || identityScoped[operationID] {
			return next(ctx, w, r, request)
		}
		ws, err := s.workspace(ctx, name)
		if err != nil {
			return nil, err
		}
		return next(context.WithValue(ctx, workspaceKey{}, ws), w, r, request)
	}
}

func workspaceFrom(ctx context.Context) identity.Workspace {
	ws, _ := ctx.Value(workspaceKey{}).(identity.Workspace)
	return ws
}

func (s *server) workspace(ctx context.Context, name string) (identity.Workspace, error) {
	if c, ok := containerFrom(ctx); ok {
		return c.AuthorizeWorkspace(name)
	}
	p, ok := principalFrom(ctx)
	if !ok {
		return identity.Workspace{}, identity.ErrUnauthenticated
	}
	return s.owners.Identity.AuthorizeWorkspace(ctx, p, name)
}

// CreateSourceUpload registers a source archive or returns where to upload it.
func (s *server) CreateSourceUpload(ctx context.Context, req CreateSourceUploadRequestObject) (CreateSourceUploadResponseObject, error) {
	ws := workspaceFrom(ctx)
	digest, err := storage.ParseDigest(req.Body.Sha256)
	if err != nil {
		return nil, err
	}
	upload, err := s.owners.Storage.RegisterSource(ctx, ws.ID, digest, req.Body.SizeBytes)
	if err != nil {
		return nil, err
	}
	out := CreateSourceUpload200JSONResponse{Sha256: digest.String(), Present: upload.Present}
	if upload.Upload != nil {
		out.Upload = &apitypes.UploadTarget{
			Url: upload.Upload.URL, Method: apitypes.UploadTargetMethod(upload.Upload.Method),
			Headers: upload.Upload.Headers, ExpiresAt: upload.Upload.ExpiresAt,
		}
	}
	return out, nil
}

// DeployApp makes the app's workloads match the request.
func (s *server) DeployApp(ctx context.Context, req DeployAppRequestObject) (DeployAppResponseObject, error) {
	ws := workspaceFrom(ctx)
	if err := s.checkImages(ctx, ws.ID, req.Body); err != nil {
		return nil, err
	}
	deployment, err := s.owners.Control.Deploy(ctx, ws.ID, req.App, *req.Body)
	if err != nil {
		return nil, err
	}
	for n, release := range deployment.Releases {
		if release.Spec.Http != nil || release.Spec.Kind == apitypes.WorkloadKindFunction {
			deployment.Releases[n].Url, deployment.Releases[n].InvokePath = s.invokeURLs(ws, req.App, release.Spec)
		}
		if url := s.owners.Edge.PodURL(release.Id, release.Spec); release.Spec.Pod != nil && url != "" {
			deployment.Releases[n].Url = &url
		}
	}
	return DeployApp200JSONResponse(deployment), nil
}

// SubmitTasks admits one task per input.
func (s *server) SubmitTasks(ctx context.Context, req SubmitTasksRequestObject) (SubmitTasksResponseObject, error) {
	ws := workspaceFrom(ctx)
	submit, err := submitRequest(ctx, ws, req.App, req.Name, req.Body.Inputs, req.Body.ReleaseId, req.Body.ParentTaskId)
	if err != nil {
		return nil, err
	}
	tasks, err := s.owners.Execution.Submit(ctx, submit)
	if err != nil {
		return nil, err
	}
	out := SubmitTasks201JSONResponse{Tasks: make([]apitypes.Task, len(tasks))}
	for n, task := range tasks {
		out.Tasks[n] = execution.APITask(task)
	}
	return out, nil
}

func submitRequest(ctx context.Context, ws identity.Workspace, app, function string, in []apitypes.TaskInput, release, parent *uuid.UUID) (execution.SubmitRequest, error) {
	inputs := make([]execution.TaskInput, len(in))
	for n, input := range in {
		payload, err := payloadFrom(apitypes.Payload{
			Encoding: apitypes.PayloadEncoding(input.Encoding), Value: input.Value, Data: input.Data,
		})
		if err != nil {
			return execution.SubmitRequest{}, fmt.Errorf("input %d: %w", n, err)
		}
		inputs[n] = execution.TaskInput{Payload: payload}
		if input.DependsOn != nil {
			for _, dep := range *input.DependsOn {
				inputs[n].DependsOn = append(inputs[n].DependsOn, execution.TaskID(dep))
			}
		}
	}
	submit := execution.SubmitRequest{Workspace: ws.ID, App: app, Function: function, Inputs: inputs, Release: release}
	switch c, ok := containerFrom(ctx); {
	case ok && c.Task != nil:
		// A task spawned from inside a running task records it as parent,
		// whatever the body names.
		submit.Parent = (*execution.TaskID)(c.Task)
	case parent != nil:
		submit.Parent = (*execution.TaskID)(parent)
	}
	return submit, nil
}

// GetTask reads a task, waiting for it to finish when asked.
func (s *server) GetTask(ctx context.Context, req GetTaskRequestObject) (GetTaskResponseObject, error) {
	ws := workspaceFrom(ctx)
	wait := time.Duration(valueOr(req.Params.WaitSeconds, 0)) * time.Second
	task, err := s.owners.Execution.GetTask(ctx, s.owners.Listener, ws.ID, execution.TaskID(req.Task), wait)
	if err != nil {
		return nil, err
	}
	return GetTask200JSONResponse(execution.APITask(task)), nil
}

// GetTaskResult returns a succeeded task's value.
func (s *server) GetTaskResult(ctx context.Context, req GetTaskResultRequestObject) (GetTaskResultResponseObject, error) {
	ws := workspaceFrom(ctx)
	result, err := s.owners.Execution.TaskResult(ctx, ws.ID, execution.TaskID(req.Task))
	if err != nil {
		return nil, err
	}
	return GetTaskResult200JSONResponse(payloadOut(result)), nil
}

// WaitTasks returns the listed tasks that have finished and those the
// workspace does not have, waiting for either when asked. The schema bounds
// the ids and the wait.
func (s *server) WaitTasks(ctx context.Context, req WaitTasksRequestObject) (WaitTasksResponseObject, error) {
	ws := workspaceFrom(ctx)
	ids := make([]execution.TaskID, len(req.Body.TaskIds))
	for n, id := range req.Body.TaskIds {
		ids[n] = execution.TaskID(id)
	}
	wait := time.Duration(valueOr(req.Body.WaitSeconds, 0)) * time.Second
	found, err := s.owners.Execution.WaitTasks(ctx, s.owners.Listener, ws.ID, ids, wait)
	if err != nil {
		return nil, err
	}
	out := waitTasksResponse{
		Tasks: make([]apitypes.FinishedTask, len(found.Finished)), MissingTaskIds: taskUUIDs(found.Missing),
	}
	for n, f := range found.Finished {
		out.Tasks[n] = apitypes.FinishedTask{Task: execution.APITask(f.Task), ResultOmitted: f.ResultOmitted}
		if f.Result != nil {
			result := payloadOut(*f.Result)
			out.Tasks[n].Result = &result
		}
	}
	return out, nil
}

// waitTasksResponse writes its JSON without HTML escaping, which would grow
// a <, > or & to six bytes, so the inline budget execution counts in raw
// bytes bounds the response.
type waitTasksResponse apitypes.WaitTasksResponse

func (r waitTasksResponse) VisitWaitTasksResponse(w http.ResponseWriter) error {
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(r); err != nil {
		return fmt.Errorf("encode finished tasks: %w", err)
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, err := buf.WriteTo(w)
	return err //nolint:wrapcheck // The client went away.
}

// CancelTask cancels a queued or running task.
func (s *server) CancelTask(ctx context.Context, req CancelTaskRequestObject) (CancelTaskResponseObject, error) {
	ws := workspaceFrom(ctx)
	task, err := s.owners.Execution.CancelTask(ctx, ws.ID, execution.TaskID(req.Task))
	if err != nil {
		return nil, err
	}
	return CancelTask200JSONResponse(execution.APITask(task)), nil
}

// logHeartbeat is the longest a followed log stream stays silent: it writes a
// blank line then, so clients can apply a read timeout.
const logHeartbeat = 15 * time.Second

// StreamTaskLogs writes the task's log entries as NDJSON.
func (s *server) StreamTaskLogs(ctx context.Context, req StreamTaskLogsRequestObject) (StreamTaskLogsResponseObject, error) {
	ws := workspaceFrom(ctx)
	return s.logStream(ctx, ws, execution.LogSource{Kind: execution.LogsOfTask, ID: req.Task},
		req.Params.After, req.Params.Tail, req.Params.Follow)
}

// logStream checks the source before the response starts, so an unknown one
// is an error response rather than an empty stream.
func (s *server) logStream(ctx context.Context, ws identity.Workspace, source execution.LogSource, after *int64, tail *int, follow *bool) (written, error) {
	if err := s.owners.Execution.CheckLogSource(ctx, ws.ID, source); err != nil {
		return nil, err
	}
	query := execution.LogQuery{
		After: valueOr(after, 0), Tail: valueOr(tail, 0), Follow: valueOr(follow, false), Heartbeat: logHeartbeat,
	}
	return ndjson(s, ctx, logEntryOut, func(emit func([]execution.LogEntry) error) error {
		return s.owners.Execution.StreamLogs(ctx, s.owners.Listener, ws.ID, source, query, emit)
	}, "log stream", "source", source.Kind, "id", source.ID), nil
}

// written is a response its operation writes itself: a stream, a socket or
// a forwarded answer, once the operation's checks passed.
type written func(http.ResponseWriter) error

func (f written) VisitStreamTaskLogsResponse(w http.ResponseWriter) error       { return f(w) }
func (f written) VisitStreamWorkloadLogsResponse(w http.ResponseWriter) error   { return f(w) }
func (f written) VisitStreamContainerLogsResponse(w http.ResponseWriter) error  { return f(w) }
func (f written) VisitStreamImageBuildLogsResponse(w http.ResponseWriter) error { return f(w) }
func (f written) VisitStreamContainerOutputResponse(w http.ResponseWriter) error {
	return f(w)
}
func (f written) VisitStreamPreviewOutputResponse(w http.ResponseWriter) error { return f(w) }
func (f written) VisitListProcessesResponse(w http.ResponseWriter) error       { return f(w) }
func (f written) VisitStartProcessResponse(w http.ResponseWriter) error        { return f(w) }
func (f written) VisitGetProcessResponse(w http.ResponseWriter) error          { return f(w) }
func (f written) VisitKillProcessResponse(w http.ResponseWriter) error         { return f(w) }
func (f written) VisitListContainerFilesResponse(w http.ResponseWriter) error  { return f(w) }
func (f written) VisitDeleteContainerFileResponse(w http.ResponseWriter) error { return f(w) }
func (f written) VisitStatContainerFileResponse(w http.ResponseWriter) error   { return f(w) }
func (f written) VisitDownloadContainerFileResponse(w http.ResponseWriter) error {
	return f(w)
}
func (f written) VisitUploadContainerFileResponse(w http.ResponseWriter) error { return f(w) }
func (f written) VisitFindInContainerFilesResponse(w http.ResponseWriter) error {
	return f(w)
}
func (f written) VisitReplaceInContainerFilesResponse(w http.ResponseWriter) error {
	return f(w)
}
func (f written) VisitCreateContainerDirectoryResponse(w http.ResponseWriter) error {
	return f(w)
}
func (f written) VisitDeleteContainerDirectoryResponse(w http.ResponseWriter) error {
	return f(w)
}

// ndjson writes stream's batches as NDJSON: line maps each entry to its
// line, an empty batch is a blank heartbeat line, and each batch is flushed.
// The status is sent first, so a failure only ends the stream early, which
// is logged as what with attrs.
func ndjson[E, L any](s *server, ctx context.Context, line func(E) L, stream func(emit func([]E) error) error, what string, attrs ...any) written {
	return func(w http.ResponseWriter) error {
		w.Header().Set("Content-Type", "application/x-ndjson")
		w.WriteHeader(http.StatusOK)
		flush := http.NewResponseController(w)
		// Send the status now; a follower may wait long for the first entry.
		if err := flush.Flush(); err != nil {
			return nil //nolint:nilerr // The client is gone.
		}
		enc := json.NewEncoder(w)
		err := stream(func(batch []E) error {
			if len(batch) == 0 {
				if _, err := w.Write([]byte("\n")); err != nil {
					return fmt.Errorf("write heartbeat: %w", err)
				}
			}
			for _, entry := range batch {
				if err := enc.Encode(line(entry)); err != nil {
					return fmt.Errorf("write entry: %w", err)
				}
			}
			if err := flush.Flush(); err != nil {
				return fmt.Errorf("flush entries: %w", err)
			}
			return nil
		})
		if err != nil && ctx.Err() == nil {
			s.logger.WarnContext(ctx, what+" ended early", append(attrs, "error", err)...)
		}
		return nil
	}
}

func logEntryOut(e execution.LogEntry) apitypes.LogEntry {
	return apitypes.LogEntry{
		Id: e.ID, TaskId: uuid.UUID(e.Task), Attempt: e.Attempt,
		Stream: apitypes.LogEntryStream(e.Stream), Data: e.Data, Time: e.Time,
	}
}

func payloadFrom(p apitypes.Payload) (execution.Payload, error) {
	switch p.Encoding {
	case apitypes.PayloadEncodingJson:
		if p.Value == nil {
			return execution.Payload{}, fmt.Errorf("%w: a json payload needs value", errInvalidRequest)
		}
		return execution.Payload{Encoding: execution.EncodingJSON, Data: *p.Value}, nil
	case apitypes.PayloadEncodingCloudpickle:
		if p.Data == nil {
			return execution.Payload{}, fmt.Errorf("%w: a cloudpickle payload needs data", errInvalidRequest)
		}
		return execution.Payload{Encoding: execution.EncodingCloudpickle, Data: *p.Data}, nil
	}
	return execution.Payload{}, fmt.Errorf("%w: unknown encoding %q", errInvalidRequest, p.Encoding)
}

func payloadOut(p execution.Payload) apitypes.Payload {
	switch p.Encoding {
	case execution.EncodingJSON:
		value := json.RawMessage(p.Data)
		return apitypes.Payload{Encoding: apitypes.PayloadEncodingJson, Value: &value}
	case execution.EncodingCloudpickle:
		data := p.Data
		out := apitypes.Payload{Encoding: apitypes.PayloadEncodingCloudpickle, Data: &data}
		var display apitypes.ResultDisplay
		// The host session checked the display before execution kept it.
		if len(p.Display) > 0 && json.Unmarshal(p.Display, &display) == nil {
			out.Display = &display
		}
		return out
	}
	return apitypes.Payload{Encoding: apitypes.PayloadEncoding(p.Encoding)}
}
