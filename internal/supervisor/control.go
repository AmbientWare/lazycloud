package supervisor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"golang.org/x/crypto/ssh"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// control serves the control API on a Unix socket: processes, files,
// shells, SSH, port tunnels and the filesystem archive. The agent forwards
// the public container operations to it unchanged.
type control struct {
	log       *slog.Logger
	children  *children
	workspace string
	// env is the container environment every process and session starts
	// with.
	env   []string
	procs *processTable
	// ssh is nil when the container serves no SSH.
	ssh *ssh.ServerConfig

	// ctx ends every long-lived connection when the server closes.
	ctx    context.Context //nolint:containedctx // the lifetime of hijacked connections
	cancel context.CancelFunc
	server *http.Server
	// handlers counts requests in flight, including hijacked connections,
	// which Server.Close does not wait for.
	handlers sync.WaitGroup
	served   sync.WaitGroup
}

func newControl(log *slog.Logger, kids *children, sshServer *hostproto.SshServer) (*control, error) {
	c := &control{
		log: log, children: kids, workspace: workspaceDir, env: containerEnvironment(),
		procs: newProcessTable(kids),
	}
	if sshServer != nil {
		config, err := sshServerConfig(sshServer)
		if err != nil {
			return nil, err
		}
		c.ssh = config
	}
	return c, nil
}

// listen serves the control API on path until close. The socket is only for
// the agent: it is the owner's alone, and owned by the owner of its
// directory, which the agent created.
func (c *control) listen(ctx context.Context, path string) error {
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("remove stale control socket: %w", err)
	}
	listener, err := (&net.ListenConfig{}).Listen(ctx, "unix", path)
	if err != nil {
		return fmt.Errorf("listen on control socket: %w", err)
	}
	if err := ownLikeParent(path); err != nil {
		_ = listener.Close()
		return err
	}
	c.ctx, c.cancel = context.WithCancel(context.WithoutCancel(ctx))
	c.server = &http.Server{Handler: c.routes(), ReadHeaderTimeout: 10 * time.Second}
	c.served.Go(func() {
		if err := c.server.Serve(listener); !errors.Is(err, http.ErrServerClosed) {
			c.log.Error("control API stopped", "error", err)
		}
	})
	return nil
}

func ownLikeParent(path string) error {
	if err := os.Chmod(path, 0o600); err != nil {
		return fmt.Errorf("chmod control socket: %w", err)
	}
	if os.Getuid() != 0 {
		return nil
	}
	info, err := os.Stat(filepath.Dir(path))
	if err != nil {
		return fmt.Errorf("stat control socket directory: %w", err)
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return nil
	}
	if err := os.Lchown(path, int(st.Uid), int(st.Gid)); err != nil {
		return fmt.Errorf("chown control socket: %w", err)
	}
	return nil
}

// close stops the server, ends every connection and process it started, and
// waits for them.
func (c *control) close() {
	if c.server != nil {
		_ = c.server.Close()
		c.cancel()
		c.served.Wait()
		c.handlers.Wait()
	}
	c.procs.close()
}

func (c *control) routes() http.Handler {
	mux := http.NewServeMux()
	handle := func(pattern string, h func(http.ResponseWriter, *http.Request) error) {
		mux.HandleFunc(pattern, func(w http.ResponseWriter, r *http.Request) {
			c.handlers.Add(1)
			defer c.handlers.Done()
			if err := h(w, r); err != nil && !errors.Is(err, errHijacked) {
				writeError(w, err)
			}
		})
	}
	handle("GET /processes", c.listProcesses)
	handle("POST /processes", c.startProcess)
	handle("GET /processes/{id}", c.getProcess)
	handle("POST /processes/{id}/kill", c.killProcess)
	handle("GET /files", c.listFiles)
	handle("DELETE /files", c.deleteFile)
	handle("GET /files/stat", c.statFile)
	handle("GET /files/content", c.downloadFile)
	handle("PUT /files/content", c.uploadFile)
	handle("POST /files/find", c.findInFiles)
	handle("POST /files/replace", c.replaceInFiles)
	handle("POST /directories", c.createDirectory)
	handle("DELETE /directories", c.deleteDirectory)
	handle("GET /shell", c.openShell)
	handle("GET /ssh", c.openSSH)
	handle("GET /ports/{port}", c.openPort)
	handle("POST /filesystem", c.archiveFilesystem)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, pattern := mux.Handler(r); pattern == "" {
			writeError(w, apiErr(http.StatusNotFound, apitypes.NotFound, "no control operation %s %s", r.Method, r.URL.Path))
			return
		}
		mux.ServeHTTP(w, r)
	})
}

// apiError is a typed refusal with its HTTP status.
type apiError struct {
	status  int
	code    apitypes.ErrorCode
	message string
}

func (e *apiError) Error() string { return e.message }

func apiErr(status int, code apitypes.ErrorCode, format string, args ...any) *apiError {
	return &apiError{status: status, code: code, message: fmt.Sprintf(format, args...)}
}

func invalid(format string, args ...any) *apiError {
	return apiErr(http.StatusBadRequest, apitypes.InvalidRequest, format, args...)
}

// fileError maps a filesystem error to the API error a caller can act on.
func fileError(err error) error {
	var typed *apiError
	if errors.As(err, &typed) {
		return typed
	}
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return apiErr(http.StatusNotFound, apitypes.NotFound, "%s", err.Error())
	case errors.Is(err, syscall.ENOTDIR), errors.Is(err, syscall.EISDIR), errors.Is(err, syscall.EEXIST),
		errors.Is(err, syscall.ENOTEMPTY), errors.Is(err, syscall.EBUSY):
		return apiErr(http.StatusConflict, apitypes.Conflict, "%s", err.Error())
	case errors.Is(err, syscall.EACCES), errors.Is(err, syscall.EPERM), errors.Is(err, syscall.EROFS):
		return apiErr(http.StatusForbidden, apitypes.Forbidden, "%s", err.Error())
	case errors.Is(err, syscall.ENOSPC), errors.Is(err, syscall.EDQUOT), errors.Is(err, syscall.EFBIG):
		return apiErr(http.StatusRequestEntityTooLarge, apitypes.PayloadTooLarge, "%s", err.Error())
	}
	return err
}

func writeError(w http.ResponseWriter, err error) {
	var typed *apiError
	if !errors.As(err, &typed) {
		typed = apiErr(http.StatusInternalServerError, apitypes.Internal, "%s", err.Error())
	}
	writeJSON(w, typed.status, apitypes.Error{Code: typed.code, Message: typed.message})
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body) //nolint:errchkjson // the caller is gone if this fails
}

// readJSON decodes a request body into v, refusing unknown fields. An
// optional body may be empty.
func readJSON(r *http.Request, v any, optional bool) error {
	decoder := json.NewDecoder(http.MaxBytesReader(nil, r.Body, 1<<20))
	decoder.DisallowUnknownFields()
	err := decoder.Decode(v)
	switch {
	case err == nil, optional && errors.Is(err, io.EOF):
		return nil
	case errors.Is(err, io.EOF):
		return invalid("a request body is required")
	}
	return invalid("invalid request body: %v", err)
}

// resolve turns a requested path into an absolute one; relative paths are
// under the workspace.
func (c *control) resolve(path string) (string, error) {
	if path == "" {
		return "", invalid("path is required")
	}
	if len(path) > 4096 {
		return "", invalid("path is longer than 4096 bytes")
	}
	if !filepath.IsAbs(path) {
		path = filepath.Join(c.workspace, path)
	}
	return filepath.Clean(path), nil
}

func (c *control) queryPath(r *http.Request) (string, error) {
	return c.resolve(r.URL.Query().Get("path"))
}
