package api

import (
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

// Server implements the public operations. Each authorizes the workspace,
// invokes one owner and maps its result.
type Server struct {
	owners Owners
	cfg    Config
	logger *slog.Logger
}

var _ StrictServerInterface = (*Server)(nil)

func (s *Server) workspace(ctx context.Context, name string) (identity.Workspace, error) {
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

// DeployApp makes the app's functions match the request.
func (s *Server) DeployApp(ctx context.Context, req DeployAppRequestObject) (DeployAppResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if err := s.checkImages(ctx, ws.ID, *req.Body); err != nil {
		return nil, err
	}
	deployment, err := s.owners.Control.Deploy(ctx, ws.ID, req.App, *req.Body)
	if err != nil {
		return nil, err
	}
	return DeployApp200JSONResponse(deployment), nil
}

// GetFunction returns a function and its active release.
func (s *Server) GetFunction(ctx context.Context, req GetFunctionRequestObject) (GetFunctionResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	fn, err := s.owners.Control.GetFunction(ctx, ws.ID, req.App, req.Function)
	if err != nil {
		return nil, err
	}
	return GetFunction200JSONResponse(fn), nil
}

// SubmitTasks admits one task per input.
func (s *Server) SubmitTasks(ctx context.Context, req SubmitTasksRequestObject) (SubmitTasksResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	inputs := make([]execution.Payload, len(req.Body.Inputs))
	for n, input := range req.Body.Inputs {
		payload, err := payloadFrom(input)
		if err != nil {
			return nil, fmt.Errorf("input %d: %w", n, err)
		}
		inputs[n] = payload
	}
	tasks, err := s.owners.Execution.Submit(ctx, execution.SubmitRequest{
		Workspace: ws.ID, App: req.App, Function: req.Function, Inputs: inputs,
	})
	if err != nil {
		return nil, err
	}
	out := SubmitTasks201JSONResponse{Tasks: make([]apitypes.Task, len(tasks))}
	for n, task := range tasks {
		out.Tasks[n] = taskOut(task)
	}
	return out, nil
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

// StreamTaskLogs writes log entries as NDJSON, flushing each batch. A
// followed stream writes a blank line after logHeartbeat without entries.
func (s *Server) StreamTaskLogs(ctx context.Context, req StreamTaskLogsRequestObject) (StreamTaskLogsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	task := execution.TaskID(req.Task)
	// Report an unknown task as an error before the stream starts.
	if _, err := s.owners.Execution.GetTask(ctx, s.owners.Listener, ws.ID, task, 0); err != nil {
		return nil, err
	}
	stream := logStream{ctx: ctx, server: s, workspace: ws.ID, task: task}
	if req.Params.After != nil {
		stream.after = *req.Params.After
	}
	if req.Params.Follow != nil {
		stream.follow = *req.Params.Follow
	}
	return stream, nil
}

type logStream struct {
	// ctx is the request's context; the generated visitor does not pass one.
	ctx       context.Context //nolint:containedctx // Lives for one response.
	server    *Server
	workspace identity.WorkspaceID
	task      execution.TaskID
	after     int64
	follow    bool
}

func (l logStream) VisitStreamTaskLogsResponse(w http.ResponseWriter) error {
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.WriteHeader(http.StatusOK)
	flush := http.NewResponseController(w)
	// Send the status now; a follower may wait long for the first entry.
	if err := flush.Flush(); err != nil {
		return nil //nolint:nilerr // The client is gone.
	}
	enc := json.NewEncoder(w)
	err := l.server.owners.Execution.StreamLogs(l.ctx, l.server.owners.Listener, l.workspace, l.task, l.after, l.follow, logHeartbeat,
		func(batch []execution.LogEntry) error {
			if len(batch) == 0 {
				if _, err := w.Write([]byte("\n")); err != nil {
					return fmt.Errorf("write heartbeat: %w", err)
				}
			}
			for _, entry := range batch {
				if err := enc.Encode(apitypes.LogEntry{
					Id: entry.ID, Attempt: entry.Attempt, Stream: apitypes.LogEntryStream(entry.Stream),
					Data: entry.Data, Time: entry.Time,
				}); err != nil {
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
		l.server.logger.WarnContext(l.ctx, "log stream ended early", "task", l.task.String(), "error", err)
	}
	return nil
}

func payloadFrom(p apitypes.Payload) (execution.Payload, error) {
	switch p.Encoding {
	case apitypes.Json:
		if p.Value == nil {
			return execution.Payload{}, fmt.Errorf("%w: a json payload needs value", errInvalidRequest)
		}
		return execution.Payload{Encoding: execution.EncodingJSON, Data: *p.Value}, nil
	case apitypes.Cloudpickle:
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
		return apitypes.Payload{Encoding: apitypes.Json, Value: &value}
	case execution.EncodingCloudpickle:
		data := p.Data
		return apitypes.Payload{Encoding: apitypes.Cloudpickle, Data: &data}
	}
	return apitypes.Payload{Encoding: apitypes.PayloadEncoding(p.Encoding)}
}

func taskOut(t execution.Task) apitypes.Task {
	out := apitypes.Task{
		Id: uuid.UUID(t.ID), App: t.App, Function: t.Function, ReleaseId: t.Release,
		Status: apitypes.TaskStatus(t.Status), Attempts: t.Attempts,
		CreatedAt: t.CreatedAt, StartedAt: t.StartedAt, FinishedAt: t.FinishedAt,
	}
	if f := t.Failure; f != nil {
		out.Failure = &apitypes.TaskFailure{Kind: apitypes.FailureKind(f.Kind), Message: f.Message}
		if f.Type != "" {
			out.Failure.Type = &f.Type
		}
		if f.Traceback != "" {
			out.Failure.Traceback = &f.Traceback
		}
		if len(f.Exception) > 0 {
			out.Failure.Exception = &f.Exception
		}
	}
	return out
}
