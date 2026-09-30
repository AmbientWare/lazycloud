package database

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Channel names a NOTIFY channel. Payloads are resource ids.
type Channel string

const (
	// ChannelHost wakes the session serving a host; payload is the host id.
	ChannelHost Channel = "lc_host"
	// ChannelExecution wakes execution planning; payload is a release id.
	ChannelExecution Channel = "lc_execution"
	// ChannelTask wakes waiters on a task; payload is the task id.
	ChannelTask Channel = "lc_task"
	// ChannelClaim wakes claims waiting for a release's queued tasks.
	ChannelClaim Channel = "lc_claim"
)

// Listener holds one connection that LISTENs on a fixed set of channels and
// wakes subscribers. A wake means "re-read durable state"; it carries no data.
// After a reconnect every subscriber is woken because notifications sent while
// disconnected are lost.
type Listener struct {
	pool     *pgxpool.Pool
	channels []Channel
	logger   *slog.Logger

	mu   sync.Mutex
	subs map[subKey]map[*subscription]struct{}
}

type subKey struct {
	channel Channel
	payload string
}

type subscription struct {
	wake chan struct{}
}

// NewListener creates a listener for channels. Run must be started to deliver
// wake-ups.
func NewListener(pool *pgxpool.Pool, logger *slog.Logger, channels ...Channel) *Listener {
	return &Listener{
		pool:     pool,
		channels: channels,
		logger:   logger,
		subs:     map[subKey]map[*subscription]struct{}{},
	}
}

// Subscribe returns a channel that receives a value after a notification on
// channel with this payload, or with any payload when payload is empty. Wakes
// coalesce: the channel holds at most one pending value. Call cancel to stop.
func (l *Listener) Subscribe(channel Channel, payload string) (wake <-chan struct{}, cancel func()) {
	sub := &subscription{wake: make(chan struct{}, 1)}
	key := subKey{channel: channel, payload: payload}
	l.mu.Lock()
	set, ok := l.subs[key]
	if !ok {
		set = map[*subscription]struct{}{}
		l.subs[key] = set
	}
	set[sub] = struct{}{}
	l.mu.Unlock()
	return sub.wake, func() {
		l.mu.Lock()
		delete(l.subs[key], sub)
		if len(l.subs[key]) == 0 {
			delete(l.subs, key)
		}
		l.mu.Unlock()
	}
}

// Run listens until ctx ends, reconnecting after connection failures.
func (l *Listener) Run(ctx context.Context) error {
	for {
		err := l.listen(ctx)
		if ctx.Err() != nil {
			return nil //nolint:nilerr // Cancellation is the normal stop.
		}
		l.logger.WarnContext(ctx, "database listener disconnected", "error", err)
		l.wakeAll()
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(time.Second):
		}
	}
}

func (l *Listener) listen(ctx context.Context) error {
	conn, err := l.pool.Acquire(ctx)
	if err != nil {
		return fmt.Errorf("acquire listener connection: %w", err)
	}
	// A listening connection must not return to the pool with LISTEN active.
	defer conn.Hijack().Close(context.WithoutCancel(ctx)) //nolint:errcheck // Closing a dead listener connection has no recovery.
	for _, channel := range l.channels {
		if _, err := conn.Exec(ctx, "listen "+pgx.Identifier{string(channel)}.Sanitize()); err != nil {
			return fmt.Errorf("listen %s: %w", channel, err)
		}
	}
	// Anything committed before LISTEN took effect must be re-read.
	l.wakeAll()
	for {
		notification, err := conn.Conn().WaitForNotification(ctx)
		if err != nil {
			return fmt.Errorf("wait for notification: %w", err)
		}
		channel := Channel(notification.Channel)
		l.wake(subKey{channel: channel, payload: notification.Payload})
		l.wake(subKey{channel: channel})
	}
}

func (l *Listener) wake(key subKey) {
	l.mu.Lock()
	defer l.mu.Unlock()
	for sub := range l.subs[key] {
		select {
		case sub.wake <- struct{}{}:
		default:
		}
	}
}

func (l *Listener) wakeAll() {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, set := range l.subs {
		for sub := range set {
			select {
			case sub.wake <- struct{}{}:
			default:
			}
		}
	}
}
