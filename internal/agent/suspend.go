package agent

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"net"
	"os"
	"slices"
	"sync"
	"time"

	"golang.org/x/sys/unix"
)

// sleepThreshold is the smallest growth of the sleep gap counted as a sleep;
// smaller growth is clock noise.
const sleepThreshold = time.Second

// clockJumpHorizon is how far ahead the clock jump timer is armed. It only
// has to never expire.
const clockJumpHorizon = 10 * 365 * 24 * time.Hour

// sleepClock is the kernel's view of sleep. CLOCK_MONOTONIC stops while the
// machine sleeps and CLOCK_BOOTTIME does not, so their gap grows by the length
// of each sleep. The kernel treats a resume as a wall clock change, which is
// what jumped waits for.
type sleepClock interface {
	// gap is CLOCK_BOOTTIME minus CLOCK_MONOTONIC.
	gap() time.Duration
	// jumped returns once the wall clock jumped, or with an error once the
	// watch failed or ctx ended.
	jumped(ctx context.Context) error
	close() error
}

// kernelClock is a timerfd armed with TFD_TIMER_CANCEL_ON_SET: the kernel
// cancels it on every wall clock change, including a resume, which makes it
// readable at once.
type kernelClock struct {
	timer *os.File
}

// openKernelClock fails where the kernel cannot give the clock jump notice;
// without it the agent would not see a resume.
func openKernelClock() (*kernelClock, error) {
	fd, err := unix.TimerfdCreate(unix.CLOCK_REALTIME, unix.TFD_NONBLOCK|unix.TFD_CLOEXEC)
	if err != nil {
		return nil, fmt.Errorf("the agent needs a timerfd clock jump notice to see a resume from sleep: %w", err)
	}
	// A non-blocking descriptor joins the runtime poller, so a read waits
	// without holding a thread and ends on a deadline.
	c := &kernelClock{timer: os.NewFile(uintptr(fd), "clock-jumps")}
	if err := c.arm(); err != nil {
		_ = c.timer.Close()
		return nil, err
	}
	return c, nil
}

func (c *kernelClock) arm() error {
	conn, err := c.timer.SyscallConn()
	if err != nil {
		return fmt.Errorf("arm the clock jump timer: %w", err)
	}
	at := unix.NsecToTimespec(time.Now().Add(clockJumpHorizon).UnixNano())
	var setErr error
	if err := conn.Control(func(fd uintptr) {
		setErr = unix.TimerfdSettime(int(fd), unix.TFD_TIMER_ABSTIME|unix.TFD_TIMER_CANCEL_ON_SET, &unix.ItimerSpec{Value: at}, nil) //nolint:gosec // A descriptor fits an int.
	}); err != nil {
		return fmt.Errorf("arm the clock jump timer: %w", err)
	}
	if setErr != nil {
		return fmt.Errorf("arm the clock jump timer: %w", setErr)
	}
	return nil
}

func (kernelClock) gap() time.Duration {
	var boot, mono unix.Timespec
	// Both clocks exist on every kernel Go supports; reading them cannot
	// fail with valid arguments. CLOCK_BOOTTIME is read second, so the time
	// between the reads never makes the gap negative.
	_ = unix.ClockGettime(unix.CLOCK_MONOTONIC, &mono)
	_ = unix.ClockGettime(unix.CLOCK_BOOTTIME, &boot)
	return time.Duration(boot.Nano() - mono.Nano())
}

func (c *kernelClock) jumped(ctx context.Context) error {
	stop := context.AfterFunc(ctx, func() { _ = c.timer.SetReadDeadline(time.Now()) })
	defer stop()
	var buf [8]byte
	_, err := c.timer.Read(buf[:])
	if ctx.Err() != nil {
		return ctx.Err() //nolint:wrapcheck // The caller's own cancellation.
	}
	// A cancelled timer reads ECANCELED; an expired one, ten years on,
	// reads its count. Both rearm.
	if err != nil && !errors.Is(err, unix.ECANCELED) {
		return fmt.Errorf("read the clock jump timer: %w", err)
	}
	return c.arm()
}

func (c *kernelClock) close() error { return c.timer.Close() } //nolint:wrapcheck // Closing a local descriptor.

// watchSleep wakes on every wall clock jump and, when the machine slept,
// brings the agent back at once. A watch that fails ends Run, since a
// resume would then go unseen.
func (a *Agent) watchSleep(ctx context.Context) {
	last := a.clock.gap()
	for {
		if err := a.clock.jumped(ctx); err != nil {
			if ctx.Err() == nil {
				a.restart(fmt.Errorf("the clock jump watch failed, so a resume would go unseen: %w", err))
			}
			return
		}
		gap := a.clock.gap()
		slept := gap - last
		last = gap
		if slept <= sleepThreshold {
			continue
		}
		a.log.Info("resumed from sleep; reconnecting", "slept_seconds", slept.Seconds())
		a.resumed()
	}
}

// resumed ends whatever the sleep left waiting on the network: it closes the
// server sockets, whose peers are gone, and reconnects the session without
// backoff. A Spot notice whose reclaim time passed during the sleep no longer
// stands, and the watcher reads the current one at once.
func (a *Agent) resumed() {
	a.conns.closeAll()
	for _, conn := range a.serverConns {
		conn.ResetConnectBackoff()
	}
	a.mu.Lock()
	if a.interruption != nil && !a.interruption.reclaimAt.After(time.Now()) {
		a.interruption = nil
	}
	out := a.session
	a.mu.Unlock()
	signal(a.reconnectNow)
	signal(a.interruptionNow)
	if out != nil {
		out.cancel()
	}
}

// signal wakes the one waiter on ch without blocking.
func signal(ch chan struct{}) {
	select {
	case ch <- struct{}{}:
	default:
	}
}

// connTracker holds the sockets under the server connections. After a sleep
// their peers have dropped them, and TCP would take minutes to notice;
// closing them makes gRPC dial again at once.
type connTracker struct {
	dialer net.Dialer
	mu     sync.Mutex
	conns  map[*trackedConn]struct{}
}

func newConnTracker() *connTracker {
	return &connTracker{conns: map[*trackedConn]struct{}{}}
}

func (t *connTracker) dial(ctx context.Context, address string) (net.Conn, error) {
	conn, err := t.dialer.DialContext(ctx, "tcp", address)
	if err != nil {
		return nil, fmt.Errorf("dial %s: %w", address, err)
	}
	tracked := &trackedConn{Conn: conn, tracker: t}
	t.mu.Lock()
	t.conns[tracked] = struct{}{}
	t.mu.Unlock()
	return tracked, nil
}

func (t *connTracker) closeAll() {
	t.mu.Lock()
	conns := slices.Collect(maps.Keys(t.conns))
	t.mu.Unlock()
	for _, conn := range conns {
		_ = conn.Close()
	}
}

type trackedConn struct {
	net.Conn
	tracker *connTracker
}

func (c *trackedConn) Close() error {
	c.tracker.mu.Lock()
	delete(c.tracker.conns, c)
	c.tracker.mu.Unlock()
	return c.Conn.Close() //nolint:wrapcheck // net.Conn's own error.
}
