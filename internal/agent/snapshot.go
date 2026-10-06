package agent

import (
	"archive/tar"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"hash"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/moby/moby/client"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// Memory snapshots are Docker checkpoints (CRIU under runc, runsc's own
// under gVisor), archived as a tar of the checkpoint directory.
const (
	// defaultHostDeadline bounds a snapshot or filesystem publish whose
	// command names no deadline.
	defaultHostDeadline = 30 * time.Minute
	// reportGrace is how long an outcome is offered to the server after
	// the operation's own deadline.
	reportGrace = 30 * time.Second
	// maxSnapshots bounds checkpoints and their uploads at once.
	maxSnapshots = 2
	// maxSnapshotBytes and maxSnapshotEntries bound a restored archive.
	maxSnapshotBytes   = 256 << 30
	maxSnapshotEntries = 100_000
	// readyAttempt bounds one readiness request.
	readyAttempt = 5 * time.Second
)

// errCannotCheckpoint marks a runtime that cannot checkpoint at all, such as
// runc without CRIU.
var errCannotCheckpoint = errors.New("this host cannot checkpoint containers")

// restorePoint is a verified, unpacked snapshot a new container starts
// from.
type restorePoint struct {
	snapshot  string
	dir       string
	automatic bool
}

// restoreInto starts the created container id from point. Docker starts
// from a checkpoint only in the container's own checkpoint directory, so
// the unpacked snapshot moves there first; that takes an agent that runs as
// root on the Docker host, as disks already do.
func (a *Agent) restoreInto(ctx context.Context, id string, point *restorePoint) error {
	info, err := a.docker.Info(ctx, client.InfoOptions{})
	if err != nil {
		return fmt.Errorf("docker info: %w", err)
	}
	target := filepath.Join(info.Info.DockerRootDir, "containers", id, "checkpoints", point.snapshot)
	if err := moveDir(point.dir, target); err != nil {
		return err
	}
	if err := markRestore(target, id); err != nil {
		return err
	}
	return a.startDocker(ctx, id, client.ContainerStartOptions{CheckpointID: point.snapshot})
}

// restoreMarker is a file the agent adds to a checkpoint before restoring
// it. Docker uploads the checkpoint directory to containerd as one blob
// first and fails when that blob is already stored (moby#42900), as it is on
// the host that took the snapshot and on every later restore there; the
// marker names the restoring container, so each restore's blob is new.
// runsc reads only its own image files.
const restoreMarker = "lazycloud-restore"

func markRestore(dir, container string) error {
	mark := container + " " + strconv.FormatInt(time.Now().UnixNano(), 10) + "\n"
	if err := os.WriteFile(filepath.Join(dir, restoreMarker), []byte(mark), 0o600); err != nil {
		return fmt.Errorf("mark checkpoint: %w", err)
	}
	return nil
}

// moveDir renames src to dst, copying when they are on different
// filesystems.
func moveDir(src, dst string) error {
	if err := os.MkdirAll(filepath.Dir(dst), 0o700); err != nil {
		return fmt.Errorf("create checkpoint directory: %w", err)
	}
	err := os.Rename(src, dst)
	if !errors.Is(err, syscall.EXDEV) {
		if err != nil {
			return fmt.Errorf("move checkpoint: %w", err)
		}
		return nil
	}
	if err := os.CopyFS(dst, os.DirFS(src)); err != nil {
		return fmt.Errorf("copy checkpoint: %w", err)
	}
	return os.RemoveAll(src) //nolint:wrapcheck // the copy is complete
}

// coldStart records that the start could not restore snapshot.
func (c *container) coldStart(snapshot string, err error) {
	c.log.Warn("restoring a snapshot failed; starting cold", "snapshot_id", snapshot, "error", err)
	c.mu.Lock()
	c.restoreFailed = snapshot
	c.mu.Unlock()
}

// prepareRestore downloads and unpacks the snapshot to start from. An
// automatic snapshot that cannot be had starts the container cold; any
// other failure fails the start.
func (c *container) prepareRestore(ctx context.Context, restore *hostproto.SnapshotRestore) (*restorePoint, error) {
	if restore == nil {
		return nil, nil
	}
	if !c.checkpointable {
		return nil, errors.New("only a checkpointable container restores a snapshot")
	}
	point, err := c.downloadRestore(ctx, restore)
	if err == nil {
		return point, nil
	}
	if !restore.GetAutomatic() || ctx.Err() != nil {
		return nil, fmt.Errorf("restore snapshot %s: %w", restore.GetSnapshotId(), err)
	}
	c.coldStart(restore.GetSnapshotId(), err)
	return nil, nil
}

func (c *container) downloadRestore(ctx context.Context, restore *hostproto.SnapshotRestore) (*restorePoint, error) {
	id := restore.GetSnapshotId()
	if !isUUID(id) {
		return nil, fmt.Errorf("snapshot id %q is not a UUID", id)
	}
	want := restore.GetSha256()
	if !sha256Hex.MatchString(want) {
		return nil, fmt.Errorf("snapshot digest %q is not a lowercase SHA-256", want)
	}
	dir, err := filepath.Abs(filepath.Join(c.dir, "checkpoints"))
	if err != nil {
		return nil, fmt.Errorf("checkpoint directory: %w", err)
	}
	target := filepath.Join(dir, id)
	if err := os.RemoveAll(target); err != nil {
		return nil, fmt.Errorf("clear checkpoint directory: %w", err)
	}
	if err := os.MkdirAll(target, 0o700); err != nil {
		return nil, fmt.Errorf("create checkpoint directory: %w", err)
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, restore.GetUrl(), nil)
	if err != nil {
		return nil, fmt.Errorf("snapshot URL: %w", err)
	}
	response, err := c.a.http.Do(request)
	if err != nil {
		return nil, fmt.Errorf("download snapshot: %w", telemetry.RedactURL(err))
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("download snapshot: HTTP %d", response.StatusCode)
	}
	// The archive unpacks as it downloads and counts only once its digest
	// matches.
	digest := sha256.New()
	body := io.TeeReader(io.LimitReader(response.Body, maxSnapshotBytes+1), digest)
	if err := untarDir(body, target); err != nil {
		_ = os.RemoveAll(target)
		return nil, err
	}
	if _, err := io.Copy(io.Discard, body); err != nil {
		_ = os.RemoveAll(target)
		return nil, fmt.Errorf("download snapshot: %w", err)
	}
	if got := hex.EncodeToString(digest.Sum(nil)); got != want {
		_ = os.RemoveAll(target)
		return nil, fmt.Errorf("snapshot digest mismatch: got %s, want %s", got, want)
	}
	return &restorePoint{snapshot: id, dir: target, automatic: restore.GetAutomatic()}, nil
}

// snapshot starts a SnapshotContainer once per snapshot id.
func (a *Agent) snapshot(request *hostproto.SnapshotContainer) {
	if !isUUID(request.GetSnapshotId()) {
		a.log.Warn("ignoring a snapshot with an invalid id", "snapshot_id", request.GetSnapshotId())
		return
	}
	key := "snapshot:" + request.GetSnapshotId()
	if !a.claimOperation(key) {
		return
	}
	a.goOwned(func(ctx context.Context) {
		defer a.releaseOperation(key)
		deadline := hostDeadline(request.GetDeadline().AsTime(), request.GetDeadline() != nil)
		work, cancel := context.WithDeadline(ctx, deadline)
		work, span := telemetry.StartIn(work, a.tracer(), request.GetTraceparent(), "agent.snapshot", trace.WithAttributes(
			telemetry.Container(request.GetContainerId()), attribute.String("lazycloud.snapshot_id", request.GetSnapshotId())))
		size, sum, err := a.takeSnapshot(work, request)
		span.SetAttributes(attribute.Int64("lazycloud.bytes", size))
		telemetry.Fail(span, err)
		cancel()
		result := &hostproto.CompleteSnapshotRequest{ContainerId: request.GetContainerId(), SnapshotId: request.GetSnapshotId(), SizeBytes: size, Sha256: sum}
		if err != nil {
			result = &hostproto.CompleteSnapshotRequest{ContainerId: request.GetContainerId(), SnapshotId: request.GetSnapshotId(),
				Failure: err.Error(), Unsupported: errors.Is(err, errCannotCheckpoint)}
			a.log.Warn("snapshot failed", "container_id", request.GetContainerId(), "snapshot_id", request.GetSnapshotId(), "error", err)
		} else {
			a.log.Info("snapshot uploaded", "container_id", request.GetContainerId(), "snapshot_id", request.GetSnapshotId(), "bytes", size)
		}
		a.deliver(ctx, deadline, "snapshot", func(ctx context.Context) error {
			_, err := a.host.CompleteSnapshot(ctx, result)
			return err //nolint:wrapcheck // deliver inspects the status
		})
	})
}

// hostDeadline is a command's deadline, or the default from now.
func hostDeadline(deadline time.Time, set bool) time.Time {
	if !set {
		return time.Now().Add(defaultHostDeadline)
	}
	return deadline
}

// takeSnapshot checkpoints the container, leaving it running, and uploads
// the archive. It returns the archive's size and digest.
func (a *Agent) takeSnapshot(ctx context.Context, request *hostproto.SnapshotContainer) (int64, string, error) {
	c := a.lookup(request.GetContainerId())
	if c == nil || !c.reachable() {
		return 0, "", errors.New("the container does not run on this host")
	}
	select {
	case a.snapshots <- struct{}{}:
		defer func() { <-a.snapshots }()
	case <-ctx.Done():
		return 0, "", fmt.Errorf("wait for a snapshot slot: %w", ctx.Err())
	}
	if probe := request.GetReady(); probe != nil {
		if err := c.waitReady(ctx, probe); err != nil {
			return 0, "", err
		}
	}
	dir, err := filepath.Abs(filepath.Join(a.cfg.StateDir, "snapshots", request.GetSnapshotId()))
	if err != nil {
		return 0, "", fmt.Errorf("snapshot directory: %w", err)
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return 0, "", fmt.Errorf("create snapshot directory: %w", err)
	}
	id := request.GetSnapshotId()
	defer func() {
		cleanup := context.WithoutCancel(ctx)
		if _, err := a.docker.CheckpointRemove(cleanup, c.dockerName(), client.CheckpointRemoveOptions{CheckpointID: id, CheckpointDir: dir}); err != nil {
			a.log.Debug("removing a checkpoint failed", "error", err)
		}
		if err := os.RemoveAll(dir); err != nil {
			a.log.Warn("removing a snapshot directory failed", "dir", dir, "error", err)
		}
	}()
	err = telemetry.Step(ctx, "agent.checkpoint", func(ctx context.Context) error {
		reattach := c.detachForCheckpoint(ctx)
		defer reattach()
		return a.checkpoint(ctx, c, client.CheckpointCreateOptions{CheckpointID: id, CheckpointDir: dir, Exit: false})
	})
	if err != nil {
		if cannotCheckpoint(err) {
			return 0, "", fmt.Errorf("%w: %v", errCannotCheckpoint, err) //nolint:errorlint // the runtime's error is a message, not a cause to match
		}
		return 0, "", fmt.Errorf("checkpoint: %w", err)
	}
	uploadCtx, upload := telemetry.Start(ctx, "agent.snapshot_upload")
	size, sum, err := a.uploadDir(uploadCtx, filepath.Join(dir, id), request.GetUploadUrl())
	telemetry.Fail(upload, err)
	if errors.Is(err, fs.ErrPermission) && os.Geteuid() != 0 {
		// Docker writes the checkpoint as root.
		return 0, "", fmt.Errorf("%w: reading a checkpoint takes an agent running as root: %v", errCannotCheckpoint, err) //nolint:errorlint // see above
	}
	return size, sum, err
}

// detachTimeout bounds the wait for the supervisor to close its sockets.
const detachTimeout = 10 * time.Second

// detachForCheckpoint has the supervisor close its sockets on the host and
// closes the link, since no runtime checkpoints a container holding a host
// socket. The returned function opens the link again; the supervisor
// reopens its sockets once it reconnects.
func (c *container) detachForCheckpoint(ctx context.Context) func() {
	c.mu.Lock()
	l := c.link
	c.mu.Unlock()
	select {
	case <-c.detached:
	default:
	}
	l.enqueue(&hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Detach{Detach: &hostproto.Detach{}}})
	timer := time.NewTimer(detachTimeout)
	select {
	case <-c.detached:
	case <-timer.C:
		c.log.Warn("the supervisor did not detach before the checkpoint")
	case <-ctx.Done():
	}
	timer.Stop()
	l.pause()
	for _, t := range []*http.Transport{c.control, c.ports, c.requests} {
		if t != nil {
			t.CloseIdleConnections()
		}
	}
	return func() {
		if err := l.resume(c.work); err != nil {
			c.log.Error("reopening the link after a checkpoint failed", "error", err)
		}
	}
}

// checkpoint checkpoints c, retrying briefly while the sandbox still
// closes the sockets the supervisor released.
func (a *Agent) checkpoint(ctx context.Context, c *container, options client.CheckpointCreateOptions) error {
	for attempt := 1; ; attempt++ {
		_, err := a.docker.CheckpointCreate(ctx, c.dockerName(), options)
		if err == nil || attempt == 3 || !strings.Contains(err.Error(), "host socket") {
			return err //nolint:wrapcheck // Wrapped by the caller.
		}
		select {
		case <-ctx.Done():
			return err //nolint:wrapcheck // see above
		case <-time.After(300 * time.Millisecond):
		}
	}
}

// cannotCheckpoint recognizes runtimes that cannot checkpoint, as opposed
// to a checkpoint that failed.
func cannotCheckpoint(err error) bool {
	message := strings.ToLower(err.Error())
	for _, sign := range []string{"criu version check failed", `"criu": executable file not found`, "only supported in experimental", "experimental feature is disabled", "not supported", "not implemented"} {
		if strings.Contains(message, sign) {
			return true
		}
	}
	return false
}

// waitReady polls the probe through the container's port tunnel until it
// answers 2xx or 3xx.
func (c *container) waitReady(ctx context.Context, probe *hostproto.ReadinessProbe) error {
	timeout := time.Duration(probe.GetTimeoutSeconds()) * time.Second
	if timeout <= 0 {
		timeout = time.Minute
	}
	interval := time.Duration(float64(probe.GetIntervalSeconds()) * float64(time.Second))
	if interval <= 0 {
		interval = time.Second
	}
	path := probe.GetPath()
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	target := fmt.Sprintf("http://127.0.0.1:%d%s", probe.GetPort(), path)
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	probeClient := &http.Client{Transport: c.ports, Timeout: readyAttempt}
	var last error
	for {
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
		if err != nil {
			return fmt.Errorf("readiness probe: %w", err)
		}
		response, err := probeClient.Do(request)
		if err == nil {
			_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 64<<10))
			_ = response.Body.Close()
			if response.StatusCode < http.StatusBadRequest {
				return nil
			}
			err = fmt.Errorf("HTTP %d", response.StatusCode)
		}
		last = err
		if !sleep(ctx, interval) {
			return fmt.Errorf("the container was not ready within %v: %w", timeout, last)
		}
	}
}

// uploadDir PUTs a tar of dir to url without holding it in memory or on
// disk: a first pass sizes and digests the archive, the second streams it
// with that length, as presigned URLs need, and must produce the same bytes.
func (a *Agent) uploadDir(ctx context.Context, dir, url string) (int64, string, error) {
	first := sha256.New()
	counted := &countingWriter{w: first}
	if err := tarDir(counted, dir); err != nil {
		return 0, "", err
	}
	size, sum := counted.n, hex.EncodeToString(first.Sum(nil))

	reader, writer := io.Pipe()
	second := sha256.New()
	written := make(chan error, 1)
	go func() {
		err := tarDir(io.MultiWriter(writer, second), dir)
		writer.CloseWithError(err)
		written <- err
	}()
	request, err := http.NewRequestWithContext(ctx, http.MethodPut, url, reader)
	if err != nil {
		_ = reader.CloseWithError(err)
		<-written
		return 0, "", fmt.Errorf("upload URL: %w", err)
	}
	request.ContentLength = size
	request.Header.Set("Content-Type", "application/x-tar")
	response, err := a.http.Do(request)
	// Unblock the writer if the request ended before reading everything.
	_ = reader.CloseWithError(errors.New("upload ended"))
	writeErr := <-written
	if err != nil {
		return 0, "", fmt.Errorf("upload snapshot: %w", telemetry.RedactURL(err))
	}
	_ = response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode > 299 {
		return 0, "", fmt.Errorf("upload snapshot: HTTP %d", response.StatusCode)
	}
	if writeErr != nil {
		return 0, "", writeErr
	}
	if hex.EncodeToString(second.Sum(nil)) != sum {
		return 0, "", errors.New("the snapshot changed while it uploaded")
	}
	return size, sum, nil
}

type countingWriter struct {
	w hash.Hash
	n int64
}

func (c *countingWriter) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	c.n += int64(n)
	return n, err //nolint:wrapcheck // hashes never fail
}

// tarDir writes dir's directories and regular files as a tar in lexical
// order, with nothing but the files' own metadata, so two passes over an
// unchanged directory write the same bytes.
func tarDir(w io.Writer, dir string) error {
	archive := tar.NewWriter(w)
	err := filepath.WalkDir(dir, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if path == dir {
			return nil
		}
		name, err := filepath.Rel(dir, path)
		if err != nil {
			return err //nolint:wrapcheck // wrapped below
		}
		info, err := entry.Info()
		if err != nil {
			return err //nolint:wrapcheck // wrapped below
		}
		if !info.IsDir() && !info.Mode().IsRegular() {
			return fmt.Errorf("%s is neither a directory nor a regular file", name)
		}
		header := &tar.Header{Name: filepath.ToSlash(name), Mode: int64(info.Mode().Perm()), ModTime: info.ModTime().Truncate(time.Second), Format: tar.FormatPAX}
		if info.IsDir() {
			header.Typeflag, header.Name = tar.TypeDir, header.Name+"/"
			return archive.WriteHeader(header) //nolint:wrapcheck // wrapped below
		}
		header.Typeflag, header.Size = tar.TypeReg, info.Size()
		if err := archive.WriteHeader(header); err != nil {
			return err //nolint:wrapcheck // wrapped below
		}
		file, err := os.Open(path) //nolint:gosec // a path under the agent's own directory
		if err != nil {
			return err //nolint:wrapcheck // wrapped below
		}
		defer func() { _ = file.Close() }()
		_, err = io.CopyN(archive, file, info.Size())
		return err //nolint:wrapcheck // wrapped below
	})
	if err == nil {
		err = archive.Close()
	}
	if err != nil {
		return fmt.Errorf("archive %s: %w", dir, err)
	}
	return nil
}

// untarDir unpacks directories and regular files into dir. Entries open
// through an os.Root, so none can leave it.
func untarDir(r io.Reader, dir string) error {
	root, err := os.OpenRoot(dir)
	if err != nil {
		return fmt.Errorf("open checkpoint directory: %w", err)
	}
	defer func() { _ = root.Close() }()
	archive := tar.NewReader(r)
	var total int64
	for entries := 0; ; entries++ {
		header, err := archive.Next()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("read snapshot archive: %w", err)
		}
		if entries >= maxSnapshotEntries {
			return fmt.Errorf("the snapshot archive has more than %d entries", maxSnapshotEntries)
		}
		name := filepath.FromSlash(strings.TrimSuffix(header.Name, "/"))
		if !filepath.IsLocal(name) {
			return fmt.Errorf("snapshot entry %q leaves its directory", header.Name)
		}
		switch header.Typeflag {
		case tar.TypeDir:
			if err := root.MkdirAll(name, 0o700); err != nil {
				return fmt.Errorf("unpack %s: %w", header.Name, err)
			}
			continue
		case tar.TypeReg:
		default:
			return fmt.Errorf("snapshot entry %q is not a regular file", header.Name)
		}
		total += header.Size
		if total > maxSnapshotBytes {
			return fmt.Errorf("the snapshot exceeds %d bytes", int64(maxSnapshotBytes))
		}
		if parent := filepath.Dir(name); parent != "." {
			if err := root.MkdirAll(parent, 0o700); err != nil {
				return fmt.Errorf("unpack %s: %w", header.Name, err)
			}
		}
		file, err := root.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_EXCL, os.FileMode(header.Mode).Perm()|0o600) //nolint:gosec // the mode is masked to permission bits
		if err != nil {
			return fmt.Errorf("unpack %s: %w", header.Name, err)
		}
		_, err = io.CopyN(file, archive, header.Size)
		if closeErr := file.Close(); err == nil {
			err = closeErr
		}
		if err != nil {
			return fmt.Errorf("unpack %s: %w", header.Name, err)
		}
	}
}

// claimOperation marks a host operation in flight, reporting false when it
// already is, so a repeated command runs it once.
func (a *Agent) claimOperation(key string) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if _, running := a.operations[key]; running {
		return false
	}
	a.operations[key] = struct{}{}
	return true
}

func (a *Agent) releaseOperation(key string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	delete(a.operations, key)
}

// deliver calls report until the server accepts or refuses it, retrying
// transient failures until reportGrace past deadline.
func (a *Agent) deliver(ctx context.Context, deadline time.Time, what string, report func(context.Context) error) {
	ctx, cancel := context.WithDeadline(ctx, deadline.Add(reportGrace))
	defer cancel()
	delay := 100 * time.Millisecond
	for {
		callCtx, callCancel := context.WithTimeout(ctx, completeCallTimeout)
		err := report(callCtx)
		callCancel()
		switch {
		case err == nil:
			return
		case status.Code(err) == codes.FailedPrecondition:
			a.log.Info("the server no longer wants this outcome", "operation", what, "error", err)
			return
		case !retryable(err):
			a.log.Error("reporting an outcome failed", "operation", what, "error", err)
			return
		}
		a.log.Warn("reporting an outcome failed; retrying", "operation", what, "error", err, "retry_in", delay)
		if !sleep(ctx, delay) {
			a.log.Error("gave up reporting an outcome", "operation", what, "error", err)
			return
		}
		delay = min(2*delay, maxCompleteBackoff)
	}
}
