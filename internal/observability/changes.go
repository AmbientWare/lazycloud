package observability

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// ChannelChanges carries committed resource changes. Statement-level
// triggers in migrations/0010_observability.sql send one notification per
// statement and workspace; see that file for the payload.
const ChannelChanges = "lc_changes"

// ChangesConfig bounds the change hub's memory.
type ChangesConfig struct {
	// Retained is how many recent events the hub keeps for resumption.
	Retained int
	// Buffer is how many events one subscriber may fall behind before it
	// is told to reset.
	Buffer int
	// MaxSubscribers bounds open streams per server.
	MaxSubscribers int
}

// DefaultChangesConfig keeps about a minute of a busy cluster's changes.
var DefaultChangesConfig = ChangesConfig{Retained: 16384, Buffer: 256, MaxSubscribers: 20000}

// ErrTooManySubscribers means the server holds MaxSubscribers streams.
var ErrTooManySubscribers = errors.New("too many open change streams")

// ChangeEvent is one notification as subscribers receive it: the changes
// one statement committed in one workspace.
type ChangeEvent struct {
	// Seq identifies the event in every server's stream; resumption names
	// it in Last-Event-ID.
	Seq       int64
	Workspace identity.WorkspaceID
	// Frame is the event rendered once as a text/event-stream frame, so
	// fan-out writes the same bytes to every subscriber.
	Frame []byte
}

// changePayload is the notification body the triggers send.
type changePayload struct {
	Seq       int64           `json:"seq"`
	Workspace uuid.UUID       `json:"workspace_id"`
	Changes   json.RawMessage `json:"changes"`
}

// Changes fans committed changes out to workspace subscribers. It listens on
// its own connection because it needs every payload in commit order and must
// know when it missed some; the shared wake listener coalesces both away.
// PostgreSQL appends notifications to one queue at commit, so every server
// sees the same order and a Last-Event-ID means the same position on each.
type Changes struct {
	pool    *pgxpool.Pool
	cfg     ChangesConfig
	logger  *slog.Logger
	metrics changeMetrics

	mu sync.Mutex
	// ring holds the last Retained events in arrival order; next is the
	// position the next one takes, and index finds an event's position by
	// seq. A gap in the notifications (a reconnect) empties all three.
	ring  []ChangeEvent
	next  uint64
	index map[int64]uint64
	subs  map[identity.WorkspaceID]map[*Subscription]struct{}
	count int
}

type changeMetrics struct {
	subscribers prometheus.Gauge
	events      prometheus.Counter
	resets      prometheus.Counter
	fanout      prometheus.Histogram
}

// NewChanges returns the hub; Run delivers notifications to it. registerer
// may be nil.
func NewChanges(pool *pgxpool.Pool, cfg ChangesConfig, registerer prometheus.Registerer, logger *slog.Logger) *Changes {
	m := changeMetrics{
		subscribers: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "lazycloud_change_stream_subscribers", Help: "Open workspace change streams.",
		}),
		events: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "lazycloud_change_events_total", Help: "Change notifications received.",
		}),
		resets: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "lazycloud_change_stream_resets_total",
			Help: "Subscribers told to reset because they fell behind or the hub missed notifications.",
		}),
		fanout: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name:    "lazycloud_change_fanout_seconds",
			Help:    "Time to hand one change event to its workspace's subscribers.",
			Buckets: prometheus.ExponentialBuckets(1e-6, 4, 10),
		}),
	}
	if registerer != nil {
		registerer.MustRegister(m.subscribers, m.events, m.resets, m.fanout)
	}
	return &Changes{
		pool: pool, cfg: cfg, logger: logger, metrics: m,
		ring:  make([]ChangeEvent, cfg.Retained),
		index: make(map[int64]uint64, cfg.Retained),
		subs:  map[identity.WorkspaceID]map[*Subscription]struct{}{},
	}
}

// Run listens until ctx ends, reconnecting after failures. Every reconnect
// is a gap: retained events are dropped and subscribers reset.
func (c *Changes) Run(ctx context.Context) error {
	for {
		err := c.listen(ctx)
		c.gap()
		if ctx.Err() != nil {
			return nil //nolint:nilerr // Cancellation is the normal stop.
		}
		c.logger.WarnContext(ctx, "change listener disconnected", "error", err)
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(time.Second):
		}
	}
}

func (c *Changes) listen(ctx context.Context) error {
	pooled, err := c.pool.Acquire(ctx)
	if err != nil {
		return fmt.Errorf("acquire change listener connection: %w", err)
	}
	conn := pooled.Hijack()
	defer conn.Close(context.WithoutCancel(ctx)) //nolint:errcheck // Closing a dead listener connection has no recovery.
	if _, err := conn.Exec(ctx, "listen "+pgx.Identifier{ChannelChanges}.Sanitize()); err != nil {
		return fmt.Errorf("listen %s: %w", ChannelChanges, err)
	}
	// Changes committed before LISTEN took effect are unknown to this hub.
	c.gap()
	for {
		n, err := conn.WaitForNotification(ctx)
		if err != nil {
			return fmt.Errorf("wait for change notification: %w", err)
		}
		var payload changePayload
		if err := json.Unmarshal([]byte(n.Payload), &payload); err != nil {
			c.logger.WarnContext(ctx, "dropping malformed change notification", "error", err)
			continue
		}
		c.Publish(ChangeEvent{
			Seq: payload.Seq, Workspace: identity.WorkspaceID(payload.Workspace),
			Frame: frame(strconv.FormatInt(payload.Seq, 10), "change", payload.Changes),
		})
	}
}

// frame renders one text/event-stream event. data holds no newline: it is
// compact JSON from PostgreSQL.
func frame(id, event string, data []byte) []byte {
	var b bytes.Buffer
	b.Grow(len(id) + len(event) + len(data) + 24)
	if id != "" {
		b.WriteString("id: ")
		b.WriteString(id)
		b.WriteByte('\n')
	}
	b.WriteString("event: ")
	b.WriteString(event)
	b.WriteString("\ndata: ")
	b.Write(bytes.ReplaceAll(data, []byte("\n"), nil))
	b.WriteString("\n\n")
	return b.Bytes()
}

// Publish retains e and hands it to its workspace's subscribers without
// waiting: a subscriber whose buffer is full is marked to reset instead.
func (c *Changes) Publish(e ChangeEvent) {
	began := time.Now()
	c.mu.Lock()
	defer c.mu.Unlock()
	c.metrics.events.Inc()
	if len(c.ring) > 0 {
		pos := c.next
		slot := &c.ring[pos%uint64(len(c.ring))]
		if slot.Frame != nil {
			delete(c.index, slot.Seq)
		}
		*slot = e
		c.index[e.Seq] = pos
		c.next++
	}
	for sub := range c.subs[e.Workspace] {
		sub.offer(e, &c.metrics)
	}
	c.metrics.fanout.Observe(time.Since(began).Seconds())
}

// gap drops retained events and resets every subscriber, because events may
// have been missed.
func (c *Changes) gap() {
	c.mu.Lock()
	defer c.mu.Unlock()
	clear(c.ring)
	clear(c.index)
	for _, set := range c.subs {
		for sub := range set {
			sub.markReset(&c.metrics)
		}
	}
}

// Subscription is one open change stream.
type Subscription struct {
	hub       *Changes
	workspace identity.WorkspaceID
	events    chan ChangeEvent
	// wake signals that reset was set.
	wake chan struct{}

	mu    sync.Mutex
	reset bool
}

func (s *Subscription) offer(e ChangeEvent, m *changeMetrics) {
	select {
	case s.events <- e:
	default:
		s.markReset(m)
	}
}

func (s *Subscription) markReset(m *changeMetrics) {
	s.mu.Lock()
	already := s.reset
	s.reset = true
	s.mu.Unlock()
	if !already {
		m.resets.Inc()
	}
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

// Events delivers the subscription's events in order.
func (s *Subscription) Events() <-chan ChangeEvent { return s.events }

// Reset receives a value when the subscriber missed events.
func (s *Subscription) Reset() <-chan struct{} { return s.wake }

// TakeReset reports whether the subscriber missed events since the last
// call, and drains the events already queued, which the reset covers.
func (s *Subscription) TakeReset() bool {
	s.mu.Lock()
	reset := s.reset
	s.reset = false
	s.mu.Unlock()
	if reset {
		for {
			select {
			case <-s.events:
				continue
			default:
			}
			break
		}
	}
	return reset
}

// Close ends the subscription.
func (s *Subscription) Close() {
	c := s.hub
	c.mu.Lock()
	defer c.mu.Unlock()
	set := c.subs[s.workspace]
	if _, ok := set[s]; !ok {
		return
	}
	delete(set, s)
	if len(set) == 0 {
		delete(c.subs, s.workspace)
	}
	c.count--
	c.metrics.subscribers.Dec()
}

// Resume is where a new subscription starts.
type Resume struct {
	// Replay holds the retained events after the requested one, for this
	// workspace, in order.
	Replay []ChangeEvent
	// Reset means the requested event is no longer retained; the client
	// must reload what it shows.
	Reset bool
	// Latest is the newest retained event's seq, which a reset reports as
	// its id so the next resumption starts there; zero when none is.
	Latest int64
}

// Subscribe opens a stream of ws's changes. With after set, it replays the
// retained events that followed it, or reports a reset when after is no
// longer retained. Registration and replay happen under one lock, so no
// event falls between them.
func (c *Changes) Subscribe(ws identity.WorkspaceID, after *int64) (*Subscription, Resume, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.count >= c.cfg.MaxSubscribers {
		return nil, Resume{}, ErrTooManySubscribers
	}
	sub := &Subscription{hub: c, workspace: ws, events: make(chan ChangeEvent, c.cfg.Buffer), wake: make(chan struct{}, 1)}
	set, ok := c.subs[ws]
	if !ok {
		set = map[*Subscription]struct{}{}
		c.subs[ws] = set
	}
	set[sub] = struct{}{}
	c.count++
	c.metrics.subscribers.Inc()

	var resume Resume
	size := uint64(len(c.ring))
	if c.next > 0 && size > 0 {
		if latest := c.ring[(c.next-1)%size]; latest.Frame != nil {
			resume.Latest = latest.Seq
		}
	}
	if after == nil {
		return sub, resume, nil
	}
	pos, ok := c.index[*after]
	if !ok {
		resume.Reset = true
		return sub, resume, nil
	}
	for p := pos + 1; p < c.next; p++ {
		if e := c.ring[p%size]; e.Workspace == ws {
			resume.Replay = append(resume.Replay, e)
		}
	}
	return sub, resume, nil
}
