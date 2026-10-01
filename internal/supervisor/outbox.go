package supervisor

import (
	"context"
	"fmt"
	"sync"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// outputLimit bounds queued output. Pipe readers stop reading above it, so a
// slow agent pushes back on the runner's writes instead of losing output.
const outputLimit = 4 << 20

// outbox is the ordered queue of messages to the agent. It outlives link
// connections: a message stays at the front until a Send succeeds. Pushes
// never block, so callers may push while holding a slot's output lock.
type outbox struct {
	mu     sync.Mutex
	items  []*hostproto.SupervisorMessage
	output int
	// wake holds a token when items were pushed.
	wake chan struct{}
	// space is closed and replaced when queued output falls below the limit.
	space chan struct{}
}

func newOutbox() *outbox {
	return &outbox{wake: make(chan struct{}, 1), space: make(chan struct{})}
}

func (o *outbox) push(m *hostproto.SupervisorMessage) {
	o.mu.Lock()
	o.items = append(o.items, m)
	o.output += len(m.GetOutput().GetData())
	o.mu.Unlock()
	select {
	case o.wake <- struct{}{}:
	default:
	}
}

func (o *outbox) front() *hostproto.SupervisorMessage {
	o.mu.Lock()
	defer o.mu.Unlock()
	if len(o.items) == 0 {
		return nil
	}
	return o.items[0]
}

// pop removes the front message after it was sent.
func (o *outbox) pop() {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.output -= len(o.items[0].GetOutput().GetData())
	o.items[0] = nil
	o.items = o.items[1:]
	if o.output < outputLimit {
		close(o.space)
		o.space = make(chan struct{})
	}
}

// unreportedAttempts lists attempts whose outcome is still queued.
func (o *outbox) unreportedAttempts() []string {
	o.mu.Lock()
	defer o.mu.Unlock()
	var attempts []string
	for _, m := range o.items {
		if finished := m.GetFinished(); finished != nil {
			attempts = append(attempts, finished.GetAttemptId())
		}
	}
	return attempts
}

func (o *outbox) empty() bool {
	o.mu.Lock()
	defer o.mu.Unlock()
	return len(o.items) == 0
}

// waitOutputSpace blocks while queued output is over the limit.
func (o *outbox) waitOutputSpace(ctx context.Context) error {
	for {
		o.mu.Lock()
		if o.output < outputLimit {
			o.mu.Unlock()
			return nil
		}
		space := o.space
		o.mu.Unlock()
		select {
		case <-space:
		case <-ctx.Done():
			return fmt.Errorf("wait for output space: %w", ctx.Err())
		}
	}
}
