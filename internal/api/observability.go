package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/observability"
)

const (
	// changeHeartbeat is the longest a change stream stays silent.
	changeHeartbeat = 15 * time.Second
	// changeStreamLifetime ends a stream so the client reconnects, which
	// authorizes the workspace again.
	changeStreamLifetime = 10 * time.Minute
	// changeWriteTimeout drops a client that stops reading.
	changeWriteTimeout = 10 * time.Second
)

// StreamChanges opens the workspace's change stream. Authorization and the
// resume point are settled before the response starts.
func (s *Server) StreamChanges(ctx context.Context, req StreamChangesRequestObject) (StreamChangesResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	var after *int64
	if id := req.Params.LastEventID; id != nil {
		seq, err := strconv.ParseInt(*id, 10, 64)
		if err != nil {
			return nil, fmt.Errorf("%w: Last-Event-ID is not an event id", errInvalidRequest)
		}
		after = &seq
	}
	sub, resume, err := s.owners.Changes.Subscribe(ws.ID, s.streamOwner(ctx), after)
	if err != nil {
		return nil, err
	}
	return changeStream{ctx: ctx, hub: s.owners.Changes, sub: sub, resume: resume}, nil
}

// streamOwner names who holds a stream, for the per-caller limit: the
// user, or the container.
func (s *Server) streamOwner(ctx context.Context) string {
	if c, ok := containerFrom(ctx); ok {
		return "container:" + c.Container.String()
	}
	p, _ := principalFrom(ctx)
	return "user:" + uuid.UUID(p.User).String()
}

// changeStream writes the subscription as text/event-stream.
type changeStream struct {
	// ctx is the request's context; the generated visitor does not pass one.
	ctx    context.Context //nolint:containedctx // Lives for one response.
	hub    *observability.Changes
	sub    *observability.Subscription
	resume observability.Resume
}

func (c changeStream) VisitStreamChangesResponse(w http.ResponseWriter) error {
	defer c.sub.Close()
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	// Proxies such as nginx otherwise buffer the stream.
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	rc := http.NewResponseController(w)
	write := func(b []byte) bool {
		_ = rc.SetWriteDeadline(time.Now().Add(changeWriteTimeout))
		_, err := w.Write(b)
		return err == nil
	}
	flush := func() bool { return rc.Flush() == nil }
	if !write([]byte(": connected\n\n")) {
		return nil
	}
	if c.resume.Reset {
		if !write(resetFrame(observability.ResetUnknownCursor, c.resume.Latest, c.resume.Latest != 0)) {
			return nil
		}
	}
	for _, e := range c.resume.Replay {
		if !write(e.Frame) {
			return nil
		}
	}
	if !flush() {
		return nil
	}
	heartbeat := time.NewTicker(changeHeartbeat)
	defer heartbeat.Stop()
	lifetime := time.NewTimer(changeStreamLifetime)
	defer lifetime.Stop()
	for {
		select {
		case <-c.ctx.Done():
			return nil
		case <-c.hub.Done():
			return nil
		case <-lifetime.C:
			return nil
		case e := <-c.sub.Events():
			if !write(e.Frame) {
				return nil
			}
			// Write whatever else is queued before one flush.
			for more := true; more; {
				select {
				case e := <-c.sub.Events():
					if !write(e.Frame) {
						return nil
					}
				default:
					more = false
				}
			}
		case <-c.sub.Reset():
			reason := c.sub.TakeReset()
			if reason == "" {
				continue
			}
			latest, ok := c.hub.Latest()
			if !write(resetFrame(reason, latest, ok)) {
				return nil
			}
		case <-heartbeat.C:
			if !write([]byte(": heartbeat\n\n")) {
				return nil
			}
		}
		if !flush() {
			return nil
		}
	}
}

// resetFrame tells the client to reload. Its id, when known, is the newest
// event the server holds, so a later resumption starts after it.
func resetFrame(reason observability.ResetReason, latest int64, known bool) []byte {
	data, _ := json.Marshal(apitypes.ChangeReset{Reason: apitypes.ChangeResetReason(reason)}) //nolint:errchkjson // A fixed struct.
	frame := "event: reset\ndata: " + string(data) + "\n\n"
	if known {
		frame = "id: " + strconv.FormatInt(latest, 10) + "\n" + frame
	}
	return []byte(frame)
}

// GetContainerMetrics returns a container's use over time.
func (s *Server) GetContainerMetrics(ctx context.Context, req GetContainerMetricsRequestObject) (GetContainerMetricsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	q := observability.MetricsQuery{Start: req.Params.Start, End: req.Params.End}
	if req.Params.StepSeconds != nil {
		step := time.Duration(*req.Params.StepSeconds) * time.Second
		q.Step = &step
	}
	metrics, err := s.owners.Observability.ContainerMetrics(ctx, ws.ID, execution.ContainerID(req.Container), q)
	if err != nil {
		return nil, err
	}
	return GetContainerMetrics200JSONResponse(metrics), nil
}

// GetContainerLifecycle returns how a container started and stopped.
func (s *Server) GetContainerLifecycle(ctx context.Context, req GetContainerLifecycleRequestObject) (GetContainerLifecycleResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	lifecycle, err := s.owners.Observability.ContainerLifecycle(ctx, ws.ID, execution.ContainerID(req.Container))
	if err != nil {
		return nil, err
	}
	return GetContainerLifecycle200JSONResponse(lifecycle), nil
}

// ListContainerLifecycles returns the lifecycles of several containers.
func (s *Server) ListContainerLifecycles(ctx context.Context, req ListContainerLifecyclesRequestObject) (ListContainerLifecyclesResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	ids := make([]execution.ContainerID, len(req.Body.ContainerIds))
	for i, id := range req.Body.ContainerIds {
		ids[i] = execution.ContainerID(id)
	}
	lifecycles, err := s.owners.Observability.ContainerLifecycles(ctx, ws.ID, ids)
	if err != nil {
		return nil, err
	}
	return ListContainerLifecycles200JSONResponse{Lifecycles: lifecycles}, nil
}

// GetTaskTimeline returns a task's submission, attempts and outcome.
func (s *Server) GetTaskTimeline(ctx context.Context, req GetTaskTimelineRequestObject) (GetTaskTimelineResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	timeline, err := s.owners.Observability.TaskTimeline(ctx, ws.ID, execution.TaskID(req.Task))
	if err != nil {
		return nil, err
	}
	return GetTaskTimeline200JSONResponse(timeline), nil
}

// GetTaskCallGraph returns the tasks of a task's call graph.
func (s *Server) GetTaskCallGraph(ctx context.Context, req GetTaskCallGraphRequestObject) (GetTaskCallGraphResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	graph, err := s.owners.Observability.TaskCallGraph(ctx, ws.ID, execution.TaskID(req.Task))
	if err != nil {
		return nil, err
	}
	return GetTaskCallGraph200JSONResponse(graph), nil
}

func rangeQuery(start, end *time.Time, windowSeconds *int) observability.RangeQuery {
	q := observability.RangeQuery{Start: start, End: end}
	if windowSeconds != nil {
		q.Width = time.Duration(*windowSeconds) * time.Second
	}
	return q
}

// GetWorkloadPerformance returns a workload's latency, outcomes and cold
// starts over time.
func (s *Server) GetWorkloadPerformance(ctx context.Context, req GetWorkloadPerformanceRequestObject) (GetWorkloadPerformanceResponseObject, error) {
	ws, id, err := s.findWorkload(ctx, req.Workspace, req.App, req.Kind, req.Name)
	if err != nil {
		return nil, err
	}
	perf, err := s.owners.Observability.WorkloadPerformance(ctx, ws.ID, uuid.UUID(id),
		rangeQuery(req.Params.Start, req.Params.End, req.Params.WindowSeconds))
	if err != nil {
		return nil, err
	}
	return GetWorkloadPerformance200JSONResponse(perf), nil
}

// GetTaskMetrics summarizes the workspace's tasks over a range.
func (s *Server) GetTaskMetrics(ctx context.Context, req GetTaskMetricsRequestObject) (GetTaskMetricsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if req.Params.Function != nil && req.Params.App == nil {
		return nil, fmt.Errorf("%w: function needs app", errInvalidRequest)
	}
	metrics, err := s.owners.Observability.TaskMetrics(ctx, ws.ID, req.Params.Start, req.Params.End,
		observability.TaskFilter{App: req.Params.App, Function: req.Params.Function})
	if err != nil {
		return nil, err
	}
	return GetTaskMetrics200JSONResponse(metrics), nil
}

// GetTaskActivity counts the workspace's tasks per bucket.
func (s *Server) GetTaskActivity(ctx context.Context, req GetTaskActivityRequestObject) (GetTaskActivityResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	activity, err := s.owners.Observability.TaskActivity(ctx, ws.ID,
		rangeQuery(req.Params.Start, req.Params.End, req.Params.WindowSeconds), req.Params.App)
	if err != nil {
		return nil, err
	}
	return GetTaskActivity200JSONResponse(activity), nil
}

// memberWorkspaces are the workspaces account metrics cover: the caller's
// memberships, or only its token's workspace.
func (s *Server) memberWorkspaces(ctx context.Context) (identity.Principal, []identity.Workspace, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return identity.Principal{}, nil, err
	}
	workspaces, err := s.owners.Identity.Workspaces(ctx, p)
	if err != nil {
		return identity.Principal{}, nil, err
	}
	return p, workspaces, nil
}

// GetAccountMetrics returns live containers and concurrency for the caller.
func (s *Server) GetAccountMetrics(ctx context.Context, _ GetAccountMetricsRequestObject) (GetAccountMetricsResponseObject, error) {
	p, workspaces, err := s.memberWorkspaces(ctx)
	if err != nil {
		return nil, err
	}
	metrics, err := s.owners.Observability.AccountMetrics(ctx, p.User, workspaces)
	if err != nil {
		return nil, err
	}
	return GetAccountMetrics200JSONResponse(metrics), nil
}

// GetAccountActivity measures the caller's workspaces per app over time.
func (s *Server) GetAccountActivity(ctx context.Context, req GetAccountActivityRequestObject) (GetAccountActivityResponseObject, error) {
	_, workspaces, err := s.memberWorkspaces(ctx)
	if err != nil {
		return nil, err
	}
	q := observability.ActivityQuery{
		RangeQuery: rangeQuery(req.Params.Start, req.Params.End, req.Params.WindowSeconds), Limit: 5,
	}
	if req.Params.Measure != nil {
		q.Measure = *req.Params.Measure
	}
	if req.Params.Limit != nil {
		q.Limit = *req.Params.Limit
	}
	activity, err := s.owners.Observability.AccountActivity(ctx, workspaces, q)
	if err != nil {
		return nil, err
	}
	return GetAccountActivity200JSONResponse(activity), nil
}
