package edge

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// Pods and sandboxes answer on <release id>-<port> and <container id>-<port>
// under the edge's base: HTTP to the port, through the container's
// supervisor. A release host picks a ready container of the pod and wakes a
// cold pod; a container host reaches that container only.

const (
	// PodConnectTimeout bounds how long a connection waits for a cold pod.
	PodConnectTimeout = 175 * time.Second
	// leaseRenewal paces lease renewals for containers with traffic.
	leaseRenewal = 10 * time.Second
)

var (
	errPortNotExposed = errors.New("the port is not exposed")
	errPodStopped     = errors.New("the pod is stopped")
	errPodNotReady    = errors.New("no container of the pod became ready in time")
	errPodStartFailed = errors.New("the pod failed to start")
)

// portLabel splits <uuid>-<port>.
func portLabel(label string) (uuid.UUID, int, bool) {
	stem, digits, ok := cutLast(label, "-")
	if !ok || digits == "" || digits[0] == '0' {
		return uuid.Nil, 0, false
	}
	port, err := strconv.Atoi(digits)
	if err != nil || port < 1 || port > 65535 {
		return uuid.Nil, 0, false
	}
	id, err := uuid.Parse(stem)
	if err != nil {
		return uuid.Nil, 0, false
	}
	return id, port, true
}

// Pod is the address of a pod or sandbox port on the edge.
func (u URLs) Pod(id uuid.UUID, port string) string {
	return u.build(u.host(id.String()+"-"+port), "")
}

// PodURL is where a pod release answers: its one port, or <PORT> to fill
// in when it has several. Empty without ports.
func (e *Edge) PodURL(release uuid.UUID, spec apitypes.WorkloadSpec) string {
	ports := execution.PodPorts(spec)
	switch len(ports) {
	case 0:
		return ""
	case 1:
		if spec.Pod != nil && spec.Pod.Tcp != nil && *spec.Pod.Tcp {
			return e.tcpURL(release, ports[0])
		}
		return e.urls.Pod(release, strconv.Itoa(ports[0]))
	}
	return e.urls.Pod(release, "<PORT>")
}

// ContainerPortURL is where a container's port answers.
func (e *Edge) ContainerPortURL(container uuid.UUID, port int) string {
	return e.urls.Pod(container, strconv.Itoa(port))
}

// podTarget is a resolved pod address.
type podTarget struct {
	workspace     identity.WorkspaceID
	workspaceName string
	workload      uuid.UUID
	release       *uuid.UUID
	container     *uuid.UUID
	port          int
	spec          apitypes.WorkloadSpec
	accepting     bool
}

func (t podTarget) authorized() bool { return t.spec.Authorized != nil && *t.spec.Authorized }

// resolvePod finds what <id>-<port> names: a container of a pod or
// sandbox, else a pod release.
func (e *Edge) resolvePod(ctx context.Context, id uuid.UUID, port int) (podTarget, error) {
	route, err := e.execution.Route(ctx, execution.ContainerID(id))
	switch {
	case err == nil:
		if route.Kind != apitypes.WorkloadKindPod && route.Kind != apitypes.WorkloadKindSandbox {
			return podTarget{}, errNoRoute
		}
		if !slices.Contains(route.Ports, port) {
			return podTarget{}, errPortNotExposed
		}
		return podTarget{
			workspace: route.Workspace, workspaceName: route.WorkspaceName, workload: route.Workload,
			container: &id, port: port, spec: route.Spec, accepting: true,
		}, nil
	case !errors.Is(err, execution.ErrNotFound):
		return podTarget{}, err
	}
	release, err := e.execution.PodRelease(ctx, id)
	if errors.Is(err, execution.ErrNotFound) {
		return podTarget{}, errNoRoute
	}
	if err != nil {
		return podTarget{}, err
	}
	// A sandbox's instances are reached by container only.
	if release.Kind != apitypes.WorkloadKindPod {
		return podTarget{}, errNoRoute
	}
	if !slices.Contains(execution.PodPorts(release.Spec), port) {
		return podTarget{}, errPortNotExposed
	}
	return podTarget{
		workspace: release.Workspace, workspaceName: release.WorkspaceName, workload: release.Workload,
		release: &id, port: port, spec: release.Spec, accepting: release.Accepting,
	}, nil
}

// podContainer picks the container a connection goes to, waking a cold pod
// and waiting for it until deadline. A connection that woke the pod links
// to the start of the container it got.
func (e *Edge) podContainer(ctx context.Context, t podTarget, deadline time.Time) (execution.ContainerID, uuid.UUID, error) {
	ctx, span := telemetry.Start(ctx, "edge.pod_container", trace.WithAttributes(attribute.String("lazycloud.workload_id", t.workload.String())))
	c, woken, err := e.pickPodContainer(ctx, t, deadline)
	span.SetAttributes(attribute.Bool("lazycloud.cold", woken))
	if err == nil {
		span.SetAttributes(telemetry.Container(uuid.UUID(c.Container).String()))
		if woken {
			span.AddLink(trace.Link{SpanContext: telemetry.SpanContextOf(c.Traceparent)})
		}
	}
	telemetry.Fail(span, err)
	return c.Container, c.Host, err
}

func (e *Edge) pickPodContainer(ctx context.Context, t podTarget, deadline time.Time) (execution.PodContainer, bool, error) {
	if t.container != nil {
		route, err := e.execution.Route(ctx, execution.ContainerID(*t.container))
		if err != nil {
			return execution.PodContainer{}, false, err
		}
		if route.State != execution.ContainerReady || route.Host == nil {
			return execution.PodContainer{}, false, errBusy
		}
		return execution.PodContainer{Container: route.ID, Host: *route.Host}, false, nil
	}
	if !t.accepting {
		return execution.PodContainer{}, false, errPodStopped
	}
	woken := false
	var wokeAt time.Time
	for {
		wake := e.podWaits.subscribe(t.workload)
		ready, err := e.execution.ReadyPodContainers(ctx, t.workload)
		if err != nil {
			return execution.PodContainer{}, woken, err
		}
		pick := -1
		for n, c := range ready {
			if c.Release == *t.release {
				pick = n
				break
			}
		}
		if pick < 0 && len(ready) > 0 {
			pick = 0
		}
		if pick >= 0 {
			return ready[pick], woken, nil
		}
		if woken {
			// A start that failed since the wake fails the connection at
			// once instead of at its deadline.
			reason, err := e.execution.PodStartFailure(ctx, t.workload, wokeAt)
			if err != nil {
				return execution.PodContainer{}, woken, err
			}
			if reason != "" {
				return execution.PodContainer{}, woken, fmt.Errorf("%w: %s", errPodStartFailed, reason)
			}
		}
		if !woken {
			wokeAt = time.Now().Add(-time.Second)
			if err := e.execution.WakePod(ctx, t.workspace, t.workload); err != nil {
				var conflict *execution.ConflictError
				if errors.As(err, &conflict) {
					return execution.PodContainer{}, woken, errPodStopped
				}
				return execution.PodContainer{}, woken, err
			}
			woken = true
		}
		timer := time.NewTimer(min(time.Until(deadline), time.Second))
		select {
		case <-ctx.Done():
			timer.Stop()
			return execution.PodContainer{}, woken, fmt.Errorf("wait for the pod: %w", ctx.Err())
		case <-wake:
		case <-timer.C:
		}
		timer.Stop()
		if time.Now().After(deadline) {
			return execution.PodContainer{}, woken, errPodNotReady
		}
	}
}

// ConnectPod resolves, authorizes nothing, picks and holds a pod container
// for a connection the caller has authorized: the server's SSH tunnel.
// release ends the hold.
func (e *Edge) ConnectPod(ctx context.Context, workspace identity.WorkspaceID, workload uuid.UUID) (execution.ContainerID, uuid.UUID, func(), error) {
	t := podTarget{workspace: workspace, workload: workload, release: &uuid.Nil, accepting: true}
	container, host, err := e.podContainer(ctx, t, time.Now().Add(PodConnectTimeout))
	if err != nil {
		return execution.ContainerID{}, uuid.Nil, nil, err
	}
	return container, host, e.holds.hold(uuid.UUID(container)), nil
}

// servePod serves HTTP to a pod or sandbox port.
func (e *Edge) servePod(w http.ResponseWriter, r *http.Request, id uuid.UUID, port int) {
	ctx := r.Context()
	t, err := e.resolvePod(ctx, id, port)
	if err != nil {
		e.failPod(w, r, err)
		return
	}
	authorized := t.authorized()
	if authorized {
		if err := e.authorize(ctx, r, &workload{workspace: t.workspace, workspaceName: t.workspaceName}); err != nil {
			e.failPod(w, r, err)
			return
		}
	}
	container, host, err := e.podContainer(ctx, t, time.Now().Add(PodConnectTimeout))
	if err != nil {
		e.failPod(w, r, err)
		return
	}
	defer e.holds.hold(uuid.UUID(container))()
	out := r.Clone(ctx)
	upgrade := r.Header.Get("Upgrade") != ""
	header := e.requestHead(r, authorized, upgrade, &requestBody{stream: r.Body}, uuid.New())
	out.Header = http.Header{}
	for _, h := range header.GetHeaders() {
		out.Header.Add(h.GetName(), h.GetValue())
	}
	out.Header.Del(RequestIDHeader)
	if upgrade {
		out.Body = http.NoBody
	}
	resp, err := e.RoundTrip(ctx, host, uuid.UUID(container), Target{Port: port}, out)
	if err != nil {
		e.failPod(w, r, err)
		return
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode == http.StatusSwitchingProtocols {
		e.splice(w, resp)
		return
	}
	for name, values := range resp.Header {
		if isHop(name) && !strings.EqualFold(name, "Content-Length") {
			continue
		}
		for _, v := range values {
			w.Header().Add(name, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	rc := http.NewResponseController(w)
	buf := make([]byte, bodyChunk)
	for {
		n, err := resp.Body.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil { //nolint:gosec // the workload's own response, passed through
				return
			}
			_ = rc.Flush()
		}
		if errors.Is(err, io.EOF) {
			return
		}
		if err != nil {
			if ctx.Err() == nil {
				panic(http.ErrAbortHandler)
			}
			return
		}
	}
}

// splice completes an upgrade with the client and copies bytes both ways.
func (e *Edge) splice(w http.ResponseWriter, resp *http.Response) {
	tunnel, ok := resp.Body.(io.ReadWriteCloser)
	if !ok {
		writeError(w, http.StatusBadGateway, "the upgrade did not open a tunnel")
		return
	}
	conn, rw, err := http.NewResponseController(w).Hijack()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "the connection cannot be upgraded")
		return
	}
	defer func() { _ = conn.Close() }()
	_, _ = fmt.Fprintf(rw, "HTTP/1.1 101 Switching Protocols\r\n")
	for name, values := range resp.Header {
		for _, v := range values {
			_, _ = fmt.Fprintf(rw, "%s: %s\r\n", name, v)
		}
	}
	_, _ = rw.WriteString("\r\n")
	if err := rw.Flush(); err != nil {
		return
	}
	Splice(conn, rw.Reader, tunnel)
}

// Splice copies a client connection and a container tunnel both ways until
// either side ends, then closes both. in reads the client's bytes, which may
// be buffered ahead of conn.
func Splice(conn net.Conn, in io.Reader, tunnel io.ReadWriteCloser) {
	var wg sync.WaitGroup
	wg.Go(func() {
		_, _ = io.Copy(tunnel, in)
		if cw, ok := tunnel.(interface{ CloseWrite() error }); ok {
			_ = cw.CloseWrite()
		}
	})
	_, _ = io.Copy(conn, tunnel)
	_ = conn.Close()
	_ = tunnel.Close()
	wg.Wait()
}

func (e *Edge) failPod(w http.ResponseWriter, r *http.Request, err error) {
	switch {
	case errors.Is(err, errPortNotExposed):
		writeError(w, http.StatusNotFound, err.Error())
	case errors.Is(err, errPodStopped):
		writeError(w, http.StatusNotFound, "the pod is stopped")
	case errors.Is(err, errPodNotReady), errors.Is(err, ErrContainerUnreachable), errors.Is(err, errPodStartFailed):
		writeError(w, http.StatusServiceUnavailable, err.Error())
	default:
		e.fail(w, r, err)
	}
}

// podWaits wakes connections waiting for a pod's container when a
// container of the pod changes state.
type podWaits struct {
	mu      sync.Mutex
	waiting map[uuid.UUID]chan struct{}
}

func (p *podWaits) subscribe(workload uuid.UUID) <-chan struct{} {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.waiting == nil {
		p.waiting = map[uuid.UUID]chan struct{}{}
	}
	ch, ok := p.waiting[workload]
	if !ok {
		ch = make(chan struct{})
		p.waiting[workload] = ch
	}
	return ch
}

func (p *podWaits) wake(workload uuid.UUID) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if ch, ok := p.waiting[workload]; ok {
		close(ch)
		delete(p.waiting, workload)
	}
}

// containerHolds counts this edge's open connections per container and
// keeps a lease on each container that has any.
type containerHolds struct {
	mu     sync.Mutex
	counts map[uuid.UUID]int
	ended  map[uuid.UUID]bool
	// first carries containers whose first connection opened, so the lease
	// starts at once.
	first chan struct{}
}

func (h *containerHolds) hold(container uuid.UUID) func() {
	h.mu.Lock()
	if h.counts == nil {
		h.counts, h.ended = map[uuid.UUID]int{}, map[uuid.UUID]bool{}
	}
	h.counts[container]++
	fresh := h.counts[container] == 1
	delete(h.ended, container)
	h.mu.Unlock()
	if fresh {
		select {
		case h.first <- struct{}{}:
		default:
		}
	}
	var once sync.Once
	return func() {
		once.Do(func() {
			h.mu.Lock()
			defer h.mu.Unlock()
			if h.counts[container]--; h.counts[container] <= 0 {
				delete(h.counts, container)
				h.ended[container] = true
			}
		})
	}
}

func (h *containerHolds) take() (held, ended []execution.ContainerID) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for id := range h.counts {
		held = append(held, execution.ContainerID(id))
	}
	for id := range h.ended {
		ended = append(ended, execution.ContainerID(id))
	}
	clear(h.ended)
	return held, ended
}

// renewHolds keeps leases on containers with connections through this edge
// and ends the leases of those whose last one closed.
func (e *Edge) renewHolds(ctx context.Context) {
	ticker := time.NewTicker(leaseRenewal)
	defer ticker.Stop()
	for {
		held, ended := e.holds.take()
		if err := e.execution.HoldContainers(ctx, e.id, held); err != nil && ctx.Err() == nil {
			e.logger.WarnContext(ctx, "renewing container leases failed", "error", err)
		}
		if err := e.execution.ReleaseContainers(ctx, e.id, ended); err != nil && ctx.Err() == nil {
			e.logger.WarnContext(ctx, "ending container leases failed", "error", err)
		}
		select {
		case <-ctx.Done():
			// Connections end with the edge; their containers keep their
			// keep-warm windows.
			held, ended := e.holds.take()
			_ = e.execution.ReleaseContainers(context.WithoutCancel(ctx), e.id, append(held, ended...))
			return
		case <-ticker.C:
		case <-e.holds.first:
		}
	}
}
