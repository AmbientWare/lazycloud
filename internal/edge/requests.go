package edge

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// Endpoint and ASGI requests run as no task; the edge keeps a record of
// each instead, written in batches after the request ends, and the id it
// gives every request, sent as X-Request-Id, ties the record to what the
// workload wrote while serving it.
const (
	// RequestIDHeader carries a request's id to the workload and back.
	RequestIDHeader = "X-Request-Id"
	// requestQueue bounds finished requests waiting to be written; past it
	// records are dropped rather than slow requests.
	requestQueue = 8192
	// requestBatch and requestFlush bound one write.
	requestBatch = 500
	requestFlush = time.Second
	// requestRetention is how long records are kept.
	requestRetention = 7 * 24 * time.Hour
	// pruneInterval paces retention deletes, each of at most pruneBatch rows.
	pruneInterval = 10 * time.Minute
	pruneBatch    = 10_000
	maxPathLength = 1024
)

// requestRecord is one finished request.
type requestRecord struct {
	id, workspace, workload, release uuid.UUID
	// container is the last container offered the request, or nil.
	container                   uuid.UUID
	method, path                string
	status                      int
	started                     time.Time
	duration                    time.Duration
	requestBytes, responseBytes int64
}

// ErrRequestNotFound means no such request in the workspace.
var ErrRequestNotFound = errors.New("request not found")

// recordingWriter observes the status and bytes the caller receives.
type recordingWriter struct {
	http.ResponseWriter
	status int
	bytes  int64
}

func (w *recordingWriter) WriteHeader(code int) {
	if w.status == 0 {
		w.status = code
	}
	w.ResponseWriter.WriteHeader(code)
}

func (w *recordingWriter) Write(p []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK
	}
	n, err := w.ResponseWriter.Write(p)
	w.bytes += int64(n)
	return n, err //nolint:wrapcheck // passed through
}

// Unwrap lets http.ResponseController reach the connection.
func (w *recordingWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }

// countingBody counts the request body bytes read.
type countingBody struct {
	io.ReadCloser
	n int64
}

func (b *countingBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	b.n += int64(n)
	return n, err //nolint:wrapcheck // a body's EOF must pass through unwrapped
}

// serveRecorded serves an endpoint or ASGI request under a new request id
// and queues its record when it ends, including when the response broke
// off.
func (e *Edge) serveRecorded(w http.ResponseWriter, r *http.Request, t target, serve func(http.ResponseWriter, *http.Request, *requestRecord)) {
	id, err := uuid.NewV7()
	if err != nil {
		id = uuid.New()
	}
	path := r.URL.Path
	if len(path) > maxPathLength {
		path = path[:maxPathLength]
	}
	rec := requestRecord{
		id: id, workspace: uuid.UUID(t.workload.workspace), workload: t.workload.id, release: t.release.id,
		method: r.Method, path: path, started: time.Now(),
	}
	rw := &recordingWriter{ResponseWriter: w}
	rw.Header().Set(RequestIDHeader, id.String())
	rw.Header().Add("Access-Control-Expose-Headers", RequestIDHeader)
	var body *countingBody
	if r.Body != nil && r.Body != http.NoBody {
		body = &countingBody{ReadCloser: r.Body}
		r.Body = body
	}
	defer func() {
		rec.duration = time.Since(rec.started)
		rec.status = rw.status
		if rec.status == 0 {
			// The client left before any answer.
			rec.status = 499
		}
		rec.responseBytes = rw.bytes
		if body != nil {
			rec.requestBytes = body.n
		}
		e.queueRecord(rec)
	}()
	serve(rw, r, &rec)
}

func (e *Edge) queueRecord(rec requestRecord) {
	select {
	case e.records <- rec:
	default:
		e.droppedRecords.Add(1)
	}
}

// writeRequests writes queued records in batches and prunes old ones until
// ctx ends.
func (e *Edge) writeRequests(ctx context.Context) {
	flush := time.NewTicker(requestFlush)
	defer flush.Stop()
	prune := time.NewTicker(pruneInterval)
	defer prune.Stop()
	batch := make([]requestRecord, 0, requestBatch)
	write := func() {
		if len(batch) == 0 {
			return
		}
		if err := e.insertRequests(ctx, batch); err != nil && ctx.Err() == nil {
			e.logger.WarnContext(ctx, "write request records", "records", len(batch), "error", err)
		}
		batch = batch[:0]
		if dropped := e.droppedRecords.Swap(0); dropped > 0 {
			e.logger.WarnContext(ctx, "request records dropped while the queue was full", "records", dropped)
		}
	}
	for {
		select {
		case <-ctx.Done():
			return
		case rec := <-e.records:
			batch = append(batch, rec)
			if len(batch) >= requestBatch {
				write()
			}
		case <-flush.C:
			write()
		case <-prune.C:
			if _, err := e.queries.PruneRequests(ctx, PruneRequestsParams{
				Before: time.Now().Add(-requestRetention), MaxRows: pruneBatch,
			}); err != nil && ctx.Err() == nil {
				e.logger.WarnContext(ctx, "prune request records", "error", err)
			}
		}
	}
}

func (e *Edge) insertRequests(ctx context.Context, batch []requestRecord) error {
	p := InsertRequestsParams{}
	for _, rec := range batch {
		p.Ids = append(p.Ids, rec.id)
		p.WorkspaceIds = append(p.WorkspaceIds, rec.workspace)
		p.WorkloadIds = append(p.WorkloadIds, rec.workload)
		p.ReleaseIds = append(p.ReleaseIds, rec.release)
		p.ContainerIds = append(p.ContainerIds, rec.container)
		p.Methods = append(p.Methods, rec.method)
		p.Paths = append(p.Paths, rec.path)
		p.Statuses = append(p.Statuses, int32(rec.status)) //nolint:gosec // HTTP status codes fit
		p.StartedAts = append(p.StartedAts, rec.started)
		p.Durations = append(p.Durations, rec.duration.Milliseconds())
		p.RequestBytes = append(p.RequestBytes, rec.requestBytes)
		p.ResponseBytes = append(p.ResponseBytes, rec.responseBytes)
	}
	if err := e.queries.InsertRequests(ctx, p); err != nil {
		return fmt.Errorf("insert request records: %w", err)
	}
	return nil
}

// ListRequests returns an app's requests, newest first, optionally of one
// workload and older than before, and the cursor of the next page.
func (e *Edge) ListRequests(ctx context.Context, workspace identity.WorkspaceID, app string, name *string, before *uuid.UUID, limit int) ([]apitypes.HttpRequest, *uuid.UUID, error) {
	rows, err := e.queries.ListRequests(ctx, ListRequestsParams{
		WorkspaceID: uuid.UUID(workspace), AppName: app, Name: name, Before: before, MaxRows: int32(limit + 1), //nolint:gosec // bounded by the API
	})
	if err != nil {
		return nil, nil, fmt.Errorf("list requests: %w", err)
	}
	var next *uuid.UUID
	if len(rows) > limit {
		rows = rows[:limit]
		next = &rows[limit-1].ID
	}
	out := make([]apitypes.HttpRequest, len(rows))
	for n, row := range rows {
		out[n] = requestOut(row)
	}
	return out, next, nil
}

// Request returns one request of the workspace.
func (e *Edge) Request(ctx context.Context, workspace identity.WorkspaceID, id uuid.UUID) (apitypes.HttpRequest, error) {
	row, err := e.queries.GetRequest(ctx, GetRequestParams{ID: id, WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.HttpRequest{}, ErrRequestNotFound
	}
	if err != nil {
		return apitypes.HttpRequest{}, fmt.Errorf("read request: %w", err)
	}
	return requestOut(ListRequestsRow(row)), nil
}

func requestOut(row ListRequestsRow) apitypes.HttpRequest {
	out := apitypes.HttpRequest{
		Id: row.ID, App: row.AppName, Name: row.Name, Kind: apitypes.WorkloadKind(row.Kind), ReleaseId: row.ReleaseID,
		ContainerId: row.ContainerID, Method: row.Method, Path: row.Path, Status: int(row.Status),
		StartedAt: row.StartedAt, DurationMs: row.DurationMs, RequestBytes: row.RequestBytes, ResponseBytes: row.ResponseBytes,
	}
	if row.Version != nil {
		v := int(*row.Version)
		out.Version = &v
	}
	return out
}
