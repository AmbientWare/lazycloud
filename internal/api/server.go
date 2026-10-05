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
	"github.com/AmbientWare/lazycloud/internal/edge"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

// Server implements the public operations. Each authorizes the workspace,
// invokes one owner and maps its result.
type Server struct {
	owners Owners
	cfg    Config
	logger *slog.Logger
}

var _ StrictServerInterface = (*Server)(nil)

func (s *Server) workspace(ctx context.Context, name string) (identity.Workspace, error) {
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
func (s *Server) CreateSourceUpload(ctx context.Context, req CreateSourceUploadRequestObject) (CreateSourceUploadResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
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
func (s *Server) DeployApp(ctx context.Context, req DeployAppRequestObject) (DeployAppResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if err := s.checkImages(ctx, ws.ID, req.Body); err != nil {
		return nil, err
	}
	deployment, err := s.owners.Control.Deploy(ctx, ws.ID, req.App, *req.Body)
	if err != nil {
		return nil, err
	}
	for n, release := range deployment.Releases {
		if release.Spec.Http != nil || release.Spec.Kind == apitypes.WorkloadKindFunction {
			url := s.owners.Edge.DeployedURL(ws.ID, req.App, release.Spec)
			path := edge.InvokePath(ws.Name, req.App, release.Spec.Kind, release.Name, nil)
			deployment.Releases[n].Url, deployment.Releases[n].InvokePath = &url, &path
		}
		if url := s.owners.Edge.PodURL(release.Id, release.Spec); release.Spec.Pod != nil && url != "" {
			deployment.Releases[n].Url = &url
		}
	}
	return DeployApp200JSONResponse(deployment), nil
}

// SubmitTasks admits one task per input.
func (s *Server) SubmitTasks(ctx context.Context, req SubmitTasksRequestObject) (SubmitTasksResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
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
		out.Tasks[n] = taskOut(task)
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
func (s *Server) GetTask(ctx context.Context, req GetTaskRequestObject) (GetTaskResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	wait := time.Duration(0)
	if req.Params.WaitSeconds != nil {
		wait = time.Duration(*req.Params.WaitSeconds) * time.Second
	}
	task, err := s.owners.Execution.GetTask(ctx, s.owners.Listener, ws.ID, execution.TaskID(req.Task), wait)
	if err != nil {
		return nil, err
	}
	return GetTask200JSONResponse(taskOut(task)), nil
}

// GetTaskResult returns a succeeded task's value.
func (s *Server) GetTaskResult(ctx context.Context, req GetTaskResultRequestObject) (GetTaskResultResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	result, err := s.owners.Execution.TaskResult(ctx, ws.ID, execution.TaskID(req.Task))
	if err != nil {
		return nil, err
	}
	return GetTaskResult200JSONResponse(payloadOut(result)), nil
}

// WaitTasks returns the listed tasks that have finished and those the
// workspace does not have, waiting for either when asked. The schema bounds
// the ids and the wait.
func (s *Server) WaitTasks(ctx context.Context, req WaitTasksRequestObject) (WaitTasksResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	ids := make([]execution.TaskID, len(req.Body.TaskIds))
	for n, id := range req.Body.TaskIds {
		ids[n] = execution.TaskID(id)
	}
	wait := time.Duration(0)
	if req.Body.WaitSeconds != nil {
		wait = time.Duration(*req.Body.WaitSeconds) * time.Second
	}
	found, err := s.owners.Execution.WaitTasks(ctx, s.owners.Listener, ws.ID, ids, wait)
	if err != nil {
		return nil, err
	}
	out := waitTasksResponse{
		Tasks: make([]apitypes.FinishedTask, len(found.Finished)), MissingTaskIds: taskUUIDs(found.Missing),
	}
	for n, f := range found.Finished {
		out.Tasks[n] = apitypes.FinishedTask{Task: taskOut(f.Task), ResultOmitted: f.ResultOmitted}
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
func (s *Server) CancelTask(ctx context.Context, req CancelTaskRequestObject) (CancelTaskResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	task, err := s.owners.Execution.CancelTask(ctx, ws.ID, execution.TaskID(req.Task))
	if err != nil {
		return nil, err
	}
	return CancelTask200JSONResponse(taskOut(task)), nil
}

// logHeartbeat is the longest a followed log stream stays silent: it writes a
// blank line then, so clients can apply a read timeout.
const logHeartbeat = 15 * time.Second

// StreamTaskLogs writes the task's log entries as NDJSON.
func (s *Server) StreamTaskLogs(ctx context.Context, req StreamTaskLogsRequestObject) (StreamTaskLogsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	return s.logStream(ctx, ws, execution.LogSource{Kind: execution.LogsOfTask, ID: req.Task},
		req.Params.After, req.Params.Tail, req.Params.Follow)
}

// logStream checks the source before the response starts, so an unknown one
// is an error response rather than an empty stream.
func (s *Server) logStream(ctx context.Context, ws identity.Workspace, source execution.LogSource, after *int64, tail *int, follow *bool) (logStream, error) {
	if err := s.owners.Execution.CheckLogSource(ctx, ws.ID, source); err != nil {
		return logStream{}, err
	}
	stream := logStream{
		ctx: ctx, server: s, workspace: ws.ID, source: source,
		query: execution.LogQuery{Heartbeat: logHeartbeat},
	}
	if after != nil {
		stream.query.After = *after
	}
	if tail != nil {
		stream.query.Tail = *tail
	}
	if follow != nil {
		stream.query.Follow = *follow
	}
	return stream, nil
}

// logStream writes log entries as NDJSON, flushing each batch, and a blank
// line for each heartbeat of a followed stream.
type logStream struct {
	// ctx is the request's context; the generated visitor does not pass one.
	ctx       context.Context //nolint:containedctx // Lives for one response.
	server    *Server
	workspace identity.WorkspaceID
	source    execution.LogSource
	query     execution.LogQuery
}

func (l logStream) VisitStreamTaskLogsResponse(w http.ResponseWriter) error { return l.visit(w) }

func (l logStream) VisitStreamWorkloadLogsResponse(w http.ResponseWriter) error {
	return l.visit(w)
}

func (l logStream) VisitStreamContainerLogsResponse(w http.ResponseWriter) error {
	return l.visit(w)
}

func (l logStream) visit(w http.ResponseWriter) error {
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.WriteHeader(http.StatusOK)
	flush := http.NewResponseController(w)
	// Send the status now; a follower may wait long for the first entry.
	if err := flush.Flush(); err != nil {
		return nil //nolint:nilerr // The client is gone.
	}
	enc := json.NewEncoder(w)
	err := l.server.owners.Execution.StreamLogs(l.ctx, l.server.owners.Listener, l.workspace, l.source, l.query,
		func(batch []execution.LogEntry) error {
			if len(batch) == 0 {
				if _, err := w.Write([]byte("\n")); err != nil {
					return fmt.Errorf("write heartbeat: %w", err)
				}
			}
			for _, entry := range batch {
				if err := enc.Encode(logEntryOut(entry)); err != nil {
					return fmt.Errorf("write log entry: %w", err)
				}
			}
			if err := flush.Flush(); err != nil {
				return fmt.Errorf("flush log entries: %w", err)
			}
			return nil
		})
	// The status is sent, so a failure can only end the stream early.
	if err != nil && l.ctx.Err() == nil {
		l.server.logger.WarnContext(l.ctx, "log stream ended early", "source", l.source.Kind, "id", l.source.ID, "error", err)
	}
	return nil
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

func taskOut(t execution.Task) apitypes.Task { return execution.APITask(t) }
