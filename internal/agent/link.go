package agent

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sync"
	"time"

	"google.golang.org/grpc"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/supervisor"
)

// maxLinkQueue bounds commands waiting for a supervisor. The agent sends at
// most one RunAttempt per free slot, so the bound is only reached when a
// supervisor never connects.
const maxLinkQueue = 1024

// link serves ContainerLink on a socket only one container can reach, so a
// connection on it speaks for that container. The supervisor reconnects
// after an agent restart; the newest connection replaces older ones. Input
// from the link is untrusted: it can only affect its own container.
type link struct {
	hostproto.UnimplementedContainerLinkServer
	c        *container
	path     string
	listener net.Listener
	server   *grpc.Server

	mu    sync.Mutex
	queue []*hostproto.SupervisorCommand
	// stream is the connection that alone sends the queue; a new connection
	// supersedes it.
	stream *linkStream
	served bool
	closed bool

	goroutines sync.WaitGroup
}

// listenLink creates the container's socket. Serving starts later, so the
// socket exists before the container does.
func listenLink(ctx context.Context, c *container, dir string) (*link, error) {
	// The container may run as any user, and its supervisor creates its
	// sockets here; only this container mounts the directory, and the
	// agent's socket directory keeps everyone else out.
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, fmt.Errorf("create link directory: %w", err)
	}
	if err := os.Chmod(dir, os.ModeSticky|0o777); err != nil { //nolint:gosec // see above
		return nil, fmt.Errorf("chmod link directory: %w", err)
	}
	path := filepath.Join(dir, linkSocketName)
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("remove stale link socket: %w", err)
	}
	listener, err := (&net.ListenConfig{}).Listen(ctx, "unix", path)
	if err != nil {
		return nil, fmt.Errorf("listen on link socket: %w", err)
	}
	if err := chmodInside(dir, linkSocketName, 0o666); err != nil {
		_ = listener.Close()
		return nil, err
	}
	l := &link{
		c:        c,
		path:     path,
		listener: listener,
		server: grpc.NewServer(
			// Stop then waits for handlers, which own the receive goroutines.
			grpc.WaitForHandlers(true),
			grpc.MaxRecvMsgSize(supervisor.MaxMessageBytes),
			grpc.MaxSendMsgSize(supervisor.MaxMessageBytes),
		),
	}
	hostproto.RegisterContainerLinkServer(l.server, l)
	return l, nil
}

// chmodInside changes the mode of name in dir without following a link out
// of dir, which a running container could swap in.
func chmodInside(dir, name string, mode os.FileMode) error {
	root, err := os.OpenRoot(dir)
	if err != nil {
		return fmt.Errorf("open link directory: %w", err)
	}
	defer func() { _ = root.Close() }()
	if err := root.Chmod(name, mode); err != nil {
		return fmt.Errorf("chmod link socket: %w", err)
	}
	return nil
}

// linkStream is one supervisor connection's claim on the queue.
type linkStream struct {
	cancel context.CancelFunc
	wake   chan struct{}
}

func (l *link) serve() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.closed {
		return
	}
	l.served = true
	l.goroutines.Go(func() { _ = l.server.Serve(l.listener) })
}

// close stops the link after its current stream ends on its own, or after
// timeout. Messages the supervisor sent before exiting are handled first.
func (l *link) close(timeout time.Duration) {
	l.mu.Lock()
	if l.closed {
		l.mu.Unlock()
		return
	}
	l.closed = true
	served := l.served
	l.mu.Unlock()
	if !served {
		// The server only owns the listener once it serves it.
		_ = l.listener.Close()
	}
	stopped := make(chan struct{})
	l.goroutines.Go(func() {
		l.server.GracefulStop()
		close(stopped)
	})
	timer := time.NewTimer(timeout)
	select {
	case <-stopped:
	case <-timer.C:
		l.server.Stop()
	}
	timer.Stop()
	l.goroutines.Wait()
	_ = os.Remove(l.path)
}

func (l *link) enqueue(command *hostproto.SupervisorCommand) {
	l.mu.Lock()
	if l.closed || len(l.queue) >= maxLinkQueue {
		closed := l.closed
		l.mu.Unlock()
		l.c.log.Warn("dropping supervisor command", "closed", closed)
		return
	}
	l.queue = append(l.queue, command)
	if l.stream != nil {
		select {
		case l.stream.wake <- struct{}{}:
		default:
		}
	}
	l.mu.Unlock()
}

// queuedAttempts lists attempts sent to no supervisor yet.
func (l *link) queuedAttempts() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	var attempts []string
	for _, command := range l.queue {
		if run := command.GetRun(); run != nil {
			attempts = append(attempts, run.GetAttemptId())
		}
	}
	return attempts
}

// next returns the queue's front for s, or false once s is superseded.
func (l *link) next(s *linkStream) (*hostproto.SupervisorCommand, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.stream != s {
		return nil, false
	}
	if len(l.queue) == 0 {
		return nil, true
	}
	return l.queue[0], true
}

// sent pops the front after s delivered it. Only the current stream pops,
// so the front is what s sent. A superseded stream leaves it for its
// successor, which may send it again; the supervisor ignores a repeated
// RunAttempt.
func (l *link) sent(s *linkStream) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.stream != s {
		return false
	}
	l.queue[0] = nil
	l.queue = l.queue[1:]
	return true
}

// Connect configures the supervisor, then relays queued commands while a
// goroutine handles what the supervisor reports.
func (l *link) Connect(stream hostproto.ContainerLink_ConnectServer) error {
	ctx, cancel := context.WithCancel(stream.Context())
	defer cancel()
	s := &linkStream{cancel: cancel, wake: make(chan struct{}, 1)}
	l.mu.Lock()
	if l.stream != nil {
		l.stream.cancel()
	}
	l.stream = s
	l.mu.Unlock()
	defer func() {
		l.mu.Lock()
		if l.stream == s {
			l.stream = nil
		}
		l.mu.Unlock()
	}()

	if err := stream.Send(&hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Configure{Configure: l.c.configure()}}); err != nil {
		return fmt.Errorf("send configure: %w", err)
	}
	l.goroutines.Go(func() {
		defer cancel()
		for {
			message, err := stream.Recv()
			if err != nil {
				return
			}
			l.c.onSupervisorMessage(ctx, message)
		}
	})
	for {
		command, current := l.next(s)
		if !current {
			return nil
		}
		if command == nil {
			select {
			case <-ctx.Done():
				return nil
			case <-s.wake:
			}
			continue
		}
		if err := stream.Send(command); err != nil {
			return fmt.Errorf("send supervisor command: %w", err)
		}
		if !l.sent(s) {
			return nil
		}
	}
}
