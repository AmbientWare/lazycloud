package hostsession

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"

	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/api"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	// MaxContainerAPIBody bounds one container API request body.
	MaxContainerAPIBody = 32 << 20
	// apiChunk bounds one response body message.
	apiChunk = 256 << 10
	// TaskHeader names the task whose attempt makes a container API call.
	TaskHeader = "LazyCloud-Task"
)

// errBodyTooLarge ends a request body above MaxContainerAPIBody; the API
// answers it as payload_too_large.
var errBodyTooLarge = &http.MaxBytesError{Limit: MaxContainerAPIBody}

// ContainerAPI serves one public API request a container made. The
// container must be assigned to the calling host and not stopped; the
// request then runs as the container's principal through the same handler
// as the public API.
func (s *Server) ContainerAPI(stream grpc.BidiStreamingServer[hostproto.APIRequest, hostproto.APIResponse]) error {
	if s.config.ContainerAPI == nil {
		return status.Error(codes.Unimplemented, "the container API is not served")
	}
	ctx, cancel := s.withLifetime(stream.Context())
	defer cancel()
	first, err := stream.Recv()
	if err != nil {
		return fmt.Errorf("receive request head: %w", err)
	}
	head := first.GetHead()
	if head == nil {
		return status.Error(codes.InvalidArgument, "the first message must carry the request head")
	}
	container, err := parseContainer(head.GetContainerId())
	if err != nil {
		return err
	}
	header := http.Header{}
	for _, h := range head.GetHeaders() {
		header.Add(h.GetName(), h.GetValue())
	}
	var task *execution.TaskID
	if value := header.Get(TaskHeader); value != "" {
		id, err := uuid.Parse(value)
		if err != nil {
			return status.Errorf(codes.InvalidArgument, "%s is not a task id", TaskHeader)
		}
		task = (*execution.TaskID)(&id)
	}
	principal, err := s.execution.ContainerPrincipal(ctx, hostFrom(ctx), container, task)
	if errors.Is(err, execution.ErrTaskNotRunningHere) {
		return status.Errorf(codes.PermissionDenied, "task %s is not running on this container", header.Get(TaskHeader))
	}
	if err != nil {
		return s.grpcError(ctx, err)
	}

	body, write := io.Pipe()
	defer func() { _ = body.Close() }()
	// Recv returns once the client half-closes or this handler returns.
	s.receivers.Go(func() { write.CloseWithError(copyRequestBody(stream, first.GetBody(), write)) })
	req, err := http.NewRequestWithContext(api.WithContainer(ctx, principal), head.GetMethod(), head.GetTarget(), body)
	if err != nil {
		return status.Errorf(codes.InvalidArgument, "invalid request: %v", err)
	}
	req.Header = header
	req.Header.Del("Authorization")
	req.ContentLength = -1
	if n, err := strconv.ParseInt(header.Get("Content-Length"), 10, 64); err == nil {
		req.ContentLength = n
	}
	w := &apiResponseWriter{stream: stream, header: http.Header{}}
	s.config.ContainerAPI.ServeHTTP(w, req)
	return w.FlushError()
}

// copyRequestBody writes the request body to w until the client half-closes.
// It returns nil at the end of the body.
func copyRequestBody(stream grpc.BidiStreamingServer[hostproto.APIRequest, hostproto.APIResponse], first []byte, w io.Writer) error {
	total := 0
	chunk := first
	for {
		total += len(chunk)
		if total > MaxContainerAPIBody {
			return errBodyTooLarge
		}
		if len(chunk) > 0 {
			if _, err := w.Write(chunk); err != nil {
				return fmt.Errorf("pass request body: %w", err)
			}
		}
		msg, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("receive request body: %w", err)
		}
		chunk = msg.GetBody()
	}
}

// apiResponseWriter sends a response as a head message, then body chunks.
// Writes are buffered up to apiChunk; Flush sends what is buffered, which
// streaming operations call after each batch.
type apiResponseWriter struct {
	stream grpc.BidiStreamingServer[hostproto.APIRequest, hostproto.APIResponse]
	header http.Header
	status int
	sent   bool
	buf    bytes.Buffer
	err    error
}

func (w *apiResponseWriter) Header() http.Header { return w.header }

func (w *apiResponseWriter) WriteHeader(code int) {
	if w.status == 0 {
		w.status = code
	}
}

func (w *apiResponseWriter) Write(p []byte) (int, error) {
	if w.err != nil {
		return 0, w.err
	}
	w.WriteHeader(http.StatusOK)
	w.buf.Write(p)
	if w.buf.Len() >= apiChunk {
		if err := w.FlushError(); err != nil {
			return 0, err
		}
	}
	return len(p), nil
}

// FlushError sends the head if it is not sent yet, and the buffered body.
func (w *apiResponseWriter) FlushError() error {
	if w.err != nil {
		return w.err
	}
	w.WriteHeader(http.StatusOK)
	msg := &hostproto.APIResponse{}
	if !w.sent {
		head := &hostproto.APIResponseHead{Status: int32(w.status)} //nolint:gosec // HTTP status codes fit
		for name, values := range w.header {
			for _, value := range values {
				head.Headers = append(head.Headers, &hostproto.APIHeader{Name: name, Value: value})
			}
		}
		msg.Head = head
	}
	for w.buf.Len() > 0 || msg.Head != nil {
		msg.Body = w.buf.Next(apiChunk)
		if err := w.stream.Send(msg); err != nil {
			w.err = fmt.Errorf("send response: %w", err)
			return w.err
		}
		w.sent = true
		msg = &hostproto.APIResponse{}
	}
	return nil
}
