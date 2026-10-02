package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/coder/websocket"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/edge"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// Processes, files and shells are served by the container's supervisor.
// The server authorizes the workspace, records the call as activity and
// forwards the operation to the same path of the supervisor's control API
// over the host's data connection; status and body pass through.

// errContainerNotRunning refuses a call on a container that is not ready.
var errContainerNotRunning = errors.New("the container is not running")

const (
	// shellReadyWait bounds how long a shell waits for a new container.
	shellReadyWait = 5 * time.Minute
	// maxSocketMessage bounds one WebSocket message.
	maxSocketMessage = 1<<20 + 64
	// holdRenewal paces lease renewals for open shells and tunnels.
	holdRenewal = 10 * time.Second
)

type requestKey struct{}

// withRequest keeps the HTTP request for operations that forward it or
// upgrade its connection.
func withRequest(f StrictHandlerFunc, _ string) StrictHandlerFunc {
	return func(ctx context.Context, w http.ResponseWriter, r *http.Request, request any) (any, error) {
		return f(context.WithValue(ctx, requestKey{}, r), w, r, request)
	}
}

func requestFrom(ctx context.Context) *http.Request {
	r, _ := ctx.Value(requestKey{}).(*http.Request)
	return r
}

// liveContainer authorizes a call on a ready container of the workspace.
func (s *Server) liveContainer(ctx context.Context, workspace string, container uuid.UUID) (execution.ContainerRoute, error) {
	ws, err := s.workspace(ctx, workspace)
	if err != nil {
		return execution.ContainerRoute{}, err
	}
	route, err := s.owners.Execution.Route(ctx, execution.ContainerID(container))
	if err != nil {
		return execution.ContainerRoute{}, err
	}
	if route.Workspace != ws.ID {
		return execution.ContainerRoute{}, execution.ErrNotFound
	}
	if err := ownsInstance(ctx, route.CreatedBy); err != nil {
		return execution.ContainerRoute{}, err
	}
	if route.State != execution.ContainerReady || route.Host == nil {
		return execution.ContainerRoute{}, errContainerNotRunning
	}
	return route, s.owners.Execution.Touch(ctx, route.ID)
}

// controlCall is a forwarded control API call; it writes the supervisor's
// answer as every operation's response.
type controlCall struct {
	ctx   context.Context //nolint:containedctx // Lives for one response.
	resp  *http.Response
	owner *Server
}

// forward sends the operation's request to the container's control API.
// body replaces a request body the strict handler already decoded.
func (s *Server) forward(ctx context.Context, workspace string, container uuid.UUID, body any) (controlCall, error) {
	route, err := s.liveContainer(ctx, workspace, container)
	if err != nil {
		return controlCall{}, err
	}
	r := requestFrom(ctx)
	if r == nil {
		return controlCall{}, errors.New("the operation has no request to forward")
	}
	_, suffix, ok := strings.Cut(r.URL.Path, "/containers/"+container.String())
	if !ok {
		return controlCall{}, fmt.Errorf("%w: unexpected path %s", errInvalidRequest, r.URL.Path)
	}
	target := "http://container" + suffix
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}
	var reader io.Reader = http.NoBody
	length := int64(0)
	switch b := body.(type) {
	case nil:
	case io.Reader:
		reader, length = b, r.ContentLength
	default:
		encoded, err := json.Marshal(b)
		if err != nil {
			return controlCall{}, fmt.Errorf("encode request: %w", err)
		}
		reader, length = bytes.NewReader(encoded), int64(len(encoded))
	}
	out, err := http.NewRequestWithContext(ctx, r.Method, target, reader)
	if err != nil {
		return controlCall{}, fmt.Errorf("build control request: %w", err)
	}
	out.ContentLength = length
	if ct := r.Header.Get("Content-Type"); ct != "" {
		out.Header.Set("Content-Type", ct)
	}
	if body != nil {
		if _, ok := body.(io.Reader); !ok {
			out.Header.Set("Content-Type", "application/json")
		}
	}
	resp, err := s.owners.Edge.RoundTrip(ctx, *route.Host, container, edge.ControlAPI, out) //nolint:bodyclose // controlCall.write closes it.
	if err != nil {
		return controlCall{}, err
	}
	return controlCall{ctx: ctx, resp: resp, owner: s}, nil
}

func (c controlCall) write(w http.ResponseWriter) error {
	defer func() { _ = c.resp.Body.Close() }()
	for _, name := range []string{"Content-Type", "Content-Length", "Lazycloud-Truncated", "Content-Disposition"} {
		if v := c.resp.Header.Get(name); v != "" {
			w.Header().Set(name, v)
		}
	}
	w.WriteHeader(c.resp.StatusCode)
	if _, err := io.Copy(w, c.resp.Body); err != nil && c.ctx.Err() == nil {
		// The status is sent; dropping the connection tells the client the
		// body is incomplete.
		panic(http.ErrAbortHandler)
	}
	return nil
}

func (c controlCall) VisitListProcessesResponse(w http.ResponseWriter) error       { return c.write(w) }
func (c controlCall) VisitStartProcessResponse(w http.ResponseWriter) error        { return c.write(w) }
func (c controlCall) VisitGetProcessResponse(w http.ResponseWriter) error          { return c.write(w) }
func (c controlCall) VisitKillProcessResponse(w http.ResponseWriter) error         { return c.write(w) }
func (c controlCall) VisitListContainerFilesResponse(w http.ResponseWriter) error  { return c.write(w) }
func (c controlCall) VisitDeleteContainerFileResponse(w http.ResponseWriter) error { return c.write(w) }
func (c controlCall) VisitStatContainerFileResponse(w http.ResponseWriter) error   { return c.write(w) }
func (c controlCall) VisitDownloadContainerFileResponse(w http.ResponseWriter) error {
	return c.write(w)
}
func (c controlCall) VisitUploadContainerFileResponse(w http.ResponseWriter) error { return c.write(w) }
func (c controlCall) VisitFindInContainerFilesResponse(w http.ResponseWriter) error {
	return c.write(w)
}
func (c controlCall) VisitReplaceInContainerFilesResponse(w http.ResponseWriter) error {
	return c.write(w)
}
func (c controlCall) VisitCreateContainerDirectoryResponse(w http.ResponseWriter) error {
	return c.write(w)
}
func (c controlCall) VisitDeleteContainerDirectoryResponse(w http.ResponseWriter) error {
	return c.write(w)
}

// ListProcesses lists the container's API processes.
func (s *Server) ListProcesses(ctx context.Context, req ListProcessesRequestObject) (ListProcessesResponseObject, error) {
	return s.forward(ctx, req.Workspace, req.Container, nil)
}

// StartProcess starts a process in the container.
func (s *Server) StartProcess(ctx context.Context, req StartProcessRequestObject) (StartProcessResponseObject, error) {
	return s.forward(ctx, req.Workspace, req.Container, req.Body)
}

// GetProcess reads a process's result.
func (s *Server) GetProcess(ctx context.Context, req GetProcessRequestObject) (GetProcessResponseObject, error) {
	return s.forward(ctx, req.Workspace, req.Container, nil)
}

// KillProcess signals a process's group.
func (s *Server) KillProcess(ctx context.Context, req KillProcessRequestObject) (KillProcessResponseObject, error) {
	body := req.Body
	if body == nil {
		body = &apitypes.KillRequest{}
	}
	return s.forward(ctx, req.Workspace, req.Container, body)
}

// ListContainerFiles lists a directory.
func (s *Server) ListContainerFiles(ctx context.Context, req ListContainerFilesRequestObject) (ListContainerFilesResponseObject, error) {
	return s.forward(ctx, req.Workspace, req.Container, nil)
}

// DeleteContainerFile deletes a file.
func (s *Server) DeleteContainerFile(ctx context.Context, req DeleteContainerFileRequestObject) (DeleteContainerFileResponseObject, error) {
	return s.forward(ctx, req.Workspace, req.Container, nil)
}

// StatContainerFile reads a file's metadata.
func (s *Server) StatContainerFile(ctx context.Context, req StatContainerFileRequestObject) (StatContainerFileResponseObject, error) {
	return s.forward(ctx, req.Workspace, req.Container, nil)
}

// DownloadContainerFile streams a file's bytes.
func (s *Server) DownloadContainerFile(ctx context.Context, req DownloadContainerFileRequestObject) (DownloadContainerFileResponseObject, error) {
	return s.forward(ctx, req.Workspace, req.Container, nil)
}

// UploadContainerFile streams a file into the container.
func (s *Server) UploadContainerFile(ctx context.Context, req UploadContainerFileRequestObject) (UploadContainerFileResponseObject, error) {
	return s.forward(ctx, req.Workspace, req.Container, req.Body)
}

// FindInContainerFiles searches files.
func (s *Server) FindInContainerFiles(ctx context.Context, req FindInContainerFilesRequestObject) (FindInContainerFilesResponseObject, error) {
	return s.forward(ctx, req.Workspace, req.Container, req.Body)
}

// ReplaceInContainerFiles replaces text in files.
func (s *Server) ReplaceInContainerFiles(ctx context.Context, req ReplaceInContainerFilesRequestObject) (ReplaceInContainerFilesResponseObject, error) {
	return s.forward(ctx, req.Workspace, req.Container, req.Body)
}

// CreateContainerDirectory creates a directory.
func (s *Server) CreateContainerDirectory(ctx context.Context, req CreateContainerDirectoryRequestObject) (CreateContainerDirectoryResponseObject, error) {
	return s.forward(ctx, req.Workspace, req.Container, nil)
}

// DeleteContainerDirectory deletes a directory.
func (s *Server) DeleteContainerDirectory(ctx context.Context, req DeleteContainerDirectoryRequestObject) (DeleteContainerDirectoryResponseObject, error) {
	return s.forward(ctx, req.Workspace, req.Container, nil)
}

// StreamContainerOutput writes the container's output as NDJSON.
func (s *Server) StreamContainerOutput(ctx context.Context, req StreamContainerOutputRequestObject) (StreamContainerOutputResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if _, err := s.owners.Execution.Instance(ctx, ws.ID, execution.ContainerID(req.Container)); err != nil {
		return nil, err
	}
	if err := s.ownsContainer(ctx, execution.ContainerID(req.Container)); err != nil {
		return nil, err
	}
	out := containerOutput{ctx: ctx, server: s, workspace: ws.ID, container: execution.ContainerID(req.Container)}
	if req.Params.After != nil {
		out.after = *req.Params.After
	}
	out.follow = req.Params.Follow != nil && *req.Params.Follow
	return out, nil
}

type containerOutput struct {
	ctx       context.Context //nolint:containedctx // Lives for one response.
	server    *Server
	workspace identity.WorkspaceID
	container execution.ContainerID
	after     int64
	follow    bool
}

func (o containerOutput) VisitStreamContainerOutputResponse(w http.ResponseWriter) error {
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.WriteHeader(http.StatusOK)
	flush := http.NewResponseController(w)
	if err := flush.Flush(); err != nil {
		return nil //nolint:nilerr // The client is gone.
	}
	enc := json.NewEncoder(w)
	err := o.server.owners.Execution.StreamContainerOutput(o.ctx, o.server.owners.Listener, o.workspace, o.container, o.after, o.follow, logHeartbeat,
		func(batch []execution.ContainerLogEntry) error {
			if len(batch) == 0 {
				if _, err := w.Write([]byte("\n")); err != nil {
					return fmt.Errorf("write heartbeat: %w", err)
				}
			}
			for _, entry := range batch {
				if err := enc.Encode(apitypes.ContainerLogEntry{
					Id: entry.ID, Stream: apitypes.ContainerLogEntryStream(entry.Stream), Data: entry.Data, Time: entry.Time,
				}); err != nil {
					return fmt.Errorf("write output entry: %w", err)
				}
			}
			return flush.Flush()
		})
	if err != nil && o.ctx.Err() == nil {
		o.server.logger.WarnContext(o.ctx, "container output stream ended early", "container", o.container.String(), "error", err)
	}
	return nil
}

// acceptSocket accepts a WebSocket after checking a browser session's
// Origin, since WebSocket upgrades are GETs that the cookie check skips.
func (s *Server) acceptSocket(ctx context.Context, w http.ResponseWriter, r *http.Request) (*websocket.Conn, error) {
	if p, ok := principalFrom(ctx); ok && p.Session != nil && (s.cfg.PublicURL == "" || r.Header.Get("Origin") != s.cfg.PublicURL) {
		writeJSONError(w, http.StatusForbidden, apitypes.Forbidden, "a browser request must come from the dashboard")
		return nil, errors.New("cross-origin socket refused")
	}
	conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true, CompressionMode: websocket.CompressionDisabled})
	if err != nil {
		return nil, fmt.Errorf("accept websocket: %w", err)
	}
	conn.SetReadLimit(maxSocketMessage)
	return conn, nil
}

// holdWhile keeps a lease on container until ctx ends.
func (s *Server) holdWhile(ctx context.Context, container execution.ContainerID) func() {
	holder := uuid.New()
	ids := []execution.ContainerID{container}
	_ = s.owners.Execution.HoldContainers(ctx, holder, ids)
	done := make(chan struct{})
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		ticker := time.NewTicker(holdRenewal)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case <-ticker.C:
				_ = s.owners.Execution.HoldContainers(ctx, holder, ids)
			}
		}
	}()
	return func() {
		close(done)
		<-stopped
		_ = s.owners.Execution.ReleaseContainers(context.WithoutCancel(ctx), holder, ids)
	}
}

// OpenContainerShell opens a login shell in the container over a
// WebSocket.
func (s *Server) OpenContainerShell(ctx context.Context, req OpenContainerShellRequestObject) (OpenContainerShellResponseObject, error) {
	if err := refuseContainer(ctx); err != nil {
		return nil, err
	}
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if _, err := s.owners.Execution.Instance(ctx, ws.ID, execution.ContainerID(req.Container)); err != nil {
		return nil, err
	}
	return shellSocket{ctx: ctx, server: s, workspace: ws.ID, container: execution.ContainerID(req.Container), params: req.Params}, nil
}

type shellSocket struct {
	ctx       context.Context //nolint:containedctx // Lives for one response.
	server    *Server
	workspace identity.WorkspaceID
	container execution.ContainerID
	params    apitypes.OpenContainerShellParams
}

func (sh shellSocket) VisitOpenContainerShellResponse(w http.ResponseWriter) error {
	ctx, s := sh.ctx, sh.server
	client, err := s.acceptSocket(ctx, w, requestFrom(ctx))
	if err != nil {
		return nil //nolint:nilerr // The refusal is written.
	}
	defer func() { _ = client.CloseNow() }()
	route, err := s.waitReady(ctx, sh.container)
	if err != nil {
		sendSocketError(ctx, client, err.Error())
		_ = client.Close(websocket.StatusTryAgainLater, "the container is not running")
		return nil
	}
	defer s.holdWhile(ctx, sh.container)()
	query := ""
	if r := requestFrom(ctx); r != nil {
		query = r.URL.RawQuery
	}
	httpClient := &http.Client{Transport: containerTransport{edge: s.owners.Edge, host: *route.Host, container: uuid.UUID(sh.container)}}
	upstream, resp, err := websocket.Dial(ctx, "ws://container/shell?"+query, &websocket.DialOptions{HTTPClient: httpClient, CompressionMode: websocket.CompressionDisabled})
	if resp != nil && resp.Body != nil && err != nil {
		_ = resp.Body.Close()
	}
	if err != nil {
		sendSocketError(ctx, client, "the shell could not start: "+err.Error())
		_ = client.Close(websocket.StatusInternalError, "the shell could not start")
		return nil
	}
	defer func() { _ = upstream.CloseNow() }()
	upstream.SetReadLimit(maxSocketMessage)
	relaySockets(ctx, client, upstream)
	return nil
}

// waitReady waits for a container to become ready, as a shell to a new
// container does.
func (s *Server) waitReady(ctx context.Context, container execution.ContainerID) (execution.ContainerRoute, error) {
	deadline := time.Now().Add(shellReadyWait)
	for {
		route, err := s.owners.Execution.Route(ctx, container)
		if err != nil {
			return route, err
		}
		switch route.State {
		case execution.ContainerReady:
			if route.Host != nil {
				return route, nil
			}
		case execution.ContainerStopped, execution.ContainerDraining:
			return route, errContainerNotRunning
		case execution.ContainerPending, execution.ContainerStarting:
		}
		if time.Now().After(deadline) {
			return route, errContainerNotRunning
		}
		select {
		case <-ctx.Done():
			return route, fmt.Errorf("wait for the container: %w", ctx.Err())
		case <-time.After(250 * time.Millisecond):
		}
	}
}

// relaySockets copies messages both ways until either side closes, then
// passes the close on.
func relaySockets(ctx context.Context, client, upstream *websocket.Conn) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	copyMessages := func(from, to *websocket.Conn) {
		defer cancel()
		for {
			kind, data, err := from.Read(ctx)
			if err != nil {
				status := websocket.CloseStatus(err)
				if status == -1 {
					status = websocket.StatusNormalClosure
				}
				_ = to.Close(status, "")
				return
			}
			if err := to.Write(ctx, kind, data); err != nil {
				return
			}
		}
	}
	done := make(chan struct{})
	go func() { //nolint:gocritic // waited for below
		defer close(done)
		copyMessages(upstream, client)
	}()
	copyMessages(client, upstream)
	<-done
}

func sendSocketError(ctx context.Context, conn *websocket.Conn, message string) {
	data, _ := json.Marshal(map[string]string{"type": "error", "message": message}) //nolint:errchkjson // A string map encodes.
	_ = conn.Write(ctx, websocket.MessageText, data)
}

// containerTransport sends requests to a container's control API.
type containerTransport struct {
	edge      *edge.Edge
	host      uuid.UUID
	container uuid.UUID
}

func (t containerTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	return t.edge.RoundTrip(r.Context(), t.host, t.container, edge.ControlAPI, r) //nolint:wrapcheck // A transport passes errors through.
}

// OpenSshTunnel carries an SSH connection to a pod over a WebSocket.
func (s *Server) OpenSshTunnel(ctx context.Context, req OpenSshTunnelRequestObject) (OpenSshTunnelResponseObject, error) {
	if err := refuseContainer(ctx); err != nil {
		return nil, err
	}
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	return sshTunnel{ctx: ctx, server: s, workspace: ws.ID, app: req.App, pod: req.Name}, nil
}

type sshTunnel struct {
	ctx       context.Context //nolint:containedctx // Lives for one response.
	server    *Server
	workspace identity.WorkspaceID
	app, pod  string
}

func (t sshTunnel) VisitOpenSshTunnelResponse(w http.ResponseWriter) error {
	ctx, s := t.ctx, t.server
	// The socket opens before the pod is ready, so a proxy in front of the
	// server does not time the handshake out while a pod wakes.
	client, err := s.acceptSocket(ctx, w, requestFrom(ctx))
	if err != nil {
		return nil //nolint:nilerr // The refusal is written.
	}
	defer func() { _ = client.CloseNow() }()
	pod, err := s.owners.Execution.PodForSSH(ctx, t.workspace, t.app, t.pod)
	if err != nil {
		reason := err.Error()
		if errors.Is(err, execution.ErrNotFound) {
			reason = fmt.Sprintf("pod %s/%s not found", t.app, t.pod)
		}
		_ = client.Close(websocket.StatusPolicyViolation, truncate(reason))
		return nil
	}
	container, host, release, err := s.owners.Edge.ConnectPod(ctx, t.workspace, pod.Workload)
	if err != nil {
		_ = client.Close(websocket.StatusTryAgainLater, truncate(err.Error()))
		return nil
	}
	defer release()
	out, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://container/ssh", http.NoBody)
	if err != nil {
		return nil //nolint:nilerr // The socket closes.
	}
	out.Header.Set("Connection", "Upgrade")
	out.Header.Set("Upgrade", edge.TunnelProtocol)
	resp, err := s.owners.Edge.RoundTrip(ctx, host, uuid.UUID(container), edge.ControlAPI, out)
	if err != nil {
		_ = client.Close(websocket.StatusTryAgainLater, truncate(err.Error()))
		return nil
	}
	defer func() { _ = resp.Body.Close() }()
	tunnel, ok := resp.Body.(io.ReadWriter)
	if resp.StatusCode != http.StatusSwitchingProtocols || !ok {
		_ = client.Close(websocket.StatusInternalError, "the pod's SSH server refused the connection ("+strconv.Itoa(resp.StatusCode)+")")
		return nil
	}
	bridgeTunnel(ctx, client, tunnel)
	return nil
}

// bridgeTunnel carries WebSocket messages into a byte tunnel and the
// tunnel's bytes back as binary messages.
func bridgeTunnel(ctx context.Context, client *websocket.Conn, tunnel io.ReadWriter) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan struct{})
	go func() { //nolint:gocritic // waited for below
		defer close(done)
		defer cancel()
		buf := make([]byte, 64<<10)
		for {
			n, err := tunnel.Read(buf)
			if n > 0 {
				if werr := client.Write(ctx, websocket.MessageBinary, buf[:n]); werr != nil {
					return
				}
			}
			if err != nil {
				_ = client.Close(websocket.StatusNormalClosure, "")
				return
			}
		}
	}()
	for {
		_, data, err := client.Read(ctx)
		if err != nil {
			break
		}
		if _, err := tunnel.Write(data); err != nil {
			break
		}
	}
	if cw, ok := tunnel.(interface{ CloseWrite() error }); ok {
		_ = cw.CloseWrite()
	}
	cancel()
	<-done
}

func truncate(reason string) string {
	if len(reason) > 120 {
		return reason[:120]
	}
	return reason
}
