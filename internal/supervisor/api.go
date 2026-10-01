package supervisor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// APISocketEnv names the container API socket inside the container. The
// SDK in the container sends its API calls there.
const APISocketEnv = "LAZYCLOUD_CONTAINER_API"

const (
	// maxAPIBody bounds one request body; the agent and server enforce it
	// too.
	maxAPIBody = 32 << 20
	// maxAPICalls bounds calls in flight to the agent; later callers wait.
	maxAPICalls = 32
	apiChunk    = 256 << 10
)

// hopHeaders are connection-scoped and never forwarded. Authorization is
// dropped too: the container's identity comes from its socket.
var hopHeaders = map[string]bool{ //nolint:gochecknoglobals // constant set
	"Connection": true, "Keep-Alive": true, "Proxy-Connection": true, "Transfer-Encoding": true,
	"Te": true, "Trailer": true, "Upgrade": true, "Proxy-Authorization": true, "Authorization": true,
}

// serveAPI serves the container API on a Unix socket at path until ctx
// ends. Each request becomes one ContainerLink.API call.
func (s *Supervisor) serveAPI(ctx context.Context, path string, client hostproto.ContainerLinkClient) error {
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("remove stale API socket: %w", err)
	}
	listener, err := (&net.ListenConfig{}).Listen(ctx, "unix", path)
	if err != nil {
		return fmt.Errorf("listen on API socket: %w", err)
	}
	// Workload code may run as any user in the container.
	if err := os.Chmod(path, 0o666); err != nil { //nolint:gosec // see above
		_ = listener.Close()
		return fmt.Errorf("chmod API socket: %w", err)
	}
	proxy := &apiProxy{client: client, calls: make(chan struct{}, maxAPICalls)}
	server := &http.Server{Handler: proxy, ReadHeaderTimeout: 10 * time.Second}
	stopped := context.AfterFunc(ctx, func() { _ = server.Close() })
	defer stopped()
	if err := server.Serve(listener); !errors.Is(err, http.ErrServerClosed) {
		return fmt.Errorf("serve API socket: %w", err)
	}
	return nil
}

type apiProxy struct {
	client hostproto.ContainerLinkClient
	calls  chan struct{}
}

func writeAPIError(w http.ResponseWriter, status int, code, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"code": code, "message": message}) //nolint:errchkjson // The caller is gone if this fails.
}

func (p *apiProxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.ContentLength > maxAPIBody {
		writeAPIError(w, http.StatusRequestEntityTooLarge, "payload_too_large", "the request body exceeds 32 MiB")
		return
	}
	select {
	case p.calls <- struct{}{}:
		defer func() { <-p.calls }()
	case <-r.Context().Done():
		return
	}
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	stream, err := p.client.API(ctx, grpc.WaitForReady(true))
	if err != nil {
		writeStatusError(w, err)
		return
	}
	head := &hostproto.APIRequestHead{Method: r.Method, Target: r.URL.RequestURI()}
	for name, values := range r.Header {
		if hopHeaders[name] {
			continue
		}
		for _, value := range values {
			head.Headers = append(head.Headers, &hostproto.APIHeader{Name: name, Value: value})
		}
	}
	if err := sendBody(stream, head, r.Body); err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			writeAPIError(w, http.StatusRequestEntityTooLarge, "payload_too_large", "the request body exceeds 32 MiB")
			return
		}
		// A failed send ends the call; its status explains why.
		if _, recvErr := stream.Recv(); recvErr != nil {
			writeStatusError(w, recvErr)
			return
		}
		writeStatusError(w, err)
		return
	}
	first, err := stream.Recv()
	if err != nil {
		writeStatusError(w, err)
		return
	}
	for _, h := range first.GetHead().GetHeaders() {
		if !hopHeaders[http.CanonicalHeaderKey(h.GetName())] {
			w.Header().Add(h.GetName(), h.GetValue())
		}
	}
	w.WriteHeader(int(first.GetHead().GetStatus()))
	flush := http.NewResponseController(w)
	for msg := first; ; {
		if len(msg.GetBody()) > 0 {
			if _, err := w.Write(msg.GetBody()); err != nil {
				return
			}
			if err := flush.Flush(); err != nil {
				return
			}
		}
		if msg, err = stream.Recv(); err != nil {
			// The status is sent, so an error only ends the body early.
			return
		}
	}
}

// sendBody sends the head with the first body chunk, the rest in chunks,
// then half-closes.
func sendBody(stream hostproto.ContainerLink_APIClient, head *hostproto.APIRequestHead, body io.Reader) error {
	buf := make([]byte, apiChunk)
	total := 0
	msg := &hostproto.APIRequest{Head: head}
	for {
		n, err := io.ReadFull(body, buf)
		total += n
		if total > maxAPIBody {
			return &http.MaxBytesError{Limit: maxAPIBody}
		}
		end := errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF)
		if err != nil && !end {
			return fmt.Errorf("read request body: %w", err)
		}
		if n > 0 || msg.Head != nil {
			msg.Body = buf[:n]
			if err := stream.Send(msg); err != nil {
				return fmt.Errorf("send request: %w", err)
			}
			msg = &hostproto.APIRequest{}
		}
		if end {
			if err := stream.CloseSend(); err != nil {
				return fmt.Errorf("close request: %w", err)
			}
			return nil
		}
	}
}

// writeStatusError answers a call that failed before the server responded.
func writeStatusError(w http.ResponseWriter, err error) {
	st, _ := status.FromError(err)
	message := st.Message()
	switch st.Code() {
	case codes.PermissionDenied:
		writeAPIError(w, http.StatusForbidden, "forbidden", message)
	case codes.InvalidArgument:
		writeAPIError(w, http.StatusBadRequest, "invalid_request", message)
	case codes.Unauthenticated:
		writeAPIError(w, http.StatusUnauthorized, "unauthenticated", message)
	default:
		writeAPIError(w, http.StatusServiceUnavailable, "internal", "container API unavailable: "+message)
	}
}
