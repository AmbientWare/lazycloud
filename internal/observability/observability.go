// Package observability answers questions about what already happened:
// container metrics, start stages and lifecycles, task timelines and call
// graphs, workload performance, account metrics and the live change stream.
// It reads the rows execution and control commit and keeps only what it
// measures itself, metric samples and start stages; it decides nothing about
// tasks or containers.
package observability

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

var (
	// ErrNotFound means the task, container or deployment is not in the
	// workspace.
	ErrNotFound = errors.New("not found")
	// ErrInvalidRange means a time range is empty, reversed or holds too
	// many buckets.
	ErrInvalidRange = errors.New("invalid time range")
)

// maxBuckets bounds a bucketed range.
const maxBuckets = 500

// ConcurrencyLimits are an account's plan limits on live containers.
type ConcurrencyLimits struct {
	MaxCPUContainers int
	MaxGPUs          int
}

// LimitSource supplies an account's plan limits. Billing implements it;
// without one, account metrics report usage and leave the limits out.
type LimitSource interface {
	ConcurrencyLimits(ctx context.Context, account identity.UserID) (*ConcurrencyLimits, error)
}

// Observability is the observability owner.
type Observability struct {
	pool    *pgxpool.Pool
	queries *Queries
	limits  LimitSource
	logger  *slog.Logger
	ingest  ingestQueue
}

// Config wires the owner's optional parts.
type Config struct {
	// Limits supplies plan limits; nil leaves them out.
	Limits LimitSource
	// Registerer receives the ingest collectors; nil skips them.
	Registerer prometheus.Registerer
}

// NewObservability returns the owner over pool. sqlc's generated New
// constructs the package's Queries.
func NewObservability(pool *pgxpool.Pool, cfg Config, logger *slog.Logger) *Observability {
	return &Observability{
		pool: pool, queries: New(pool), limits: cfg.Limits, logger: logger,
		ingest: newIngestQueue(cfg.Registerer),
	}
}

// idFloor is a uuidv7 that sorts before every id generated at or after t.
// Ids and created_at come from the same clock a moment apart, so time
// ranges read the recent-first id indexes between idFloor(start - slack) and
// idFloor(end + slack) and then filter on created_at exactly.
func idFloor(t time.Time) uuid.UUID {
	var id uuid.UUID
	ms := t.UnixMilli()
	if ms < 0 {
		ms = 0
	}
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], uint64(ms))
	copy(id[:6], b[2:])
	return id
}

const idSlack = time.Minute

// span is a resolved query range.
type span struct {
	start, end time.Time
	width      time.Duration
}

func (s span) fromID() uuid.UUID { return idFloor(s.start.Add(-idSlack)) }
func (s span) toID() uuid.UUID   { return idFloor(s.end.Add(idSlack)) }

func (s span) interval() pgtype.Interval {
	return pgtype.Interval{Microseconds: s.width.Microseconds(), Valid: true}
}

// buckets lists the bucket starts of an aligned span.
func (s span) buckets() []time.Time {
	var out []time.Time
	for t := s.start; t.Before(s.end); t = t.Add(s.width) {
		out = append(out, t)
	}
	return out
}

// alignedSpan resolves an optional range into whole buckets of width
// aligned to the epoch: end defaults to now and is rounded up to include its
// bucket, start defaults to defaultBuckets before end and is rounded down.
func alignedSpan(start, end *time.Time, width time.Duration, defaultBuckets int, now time.Time) (span, error) {
	if width <= 0 {
		return span{}, fmt.Errorf("%w: the bucket width must be positive", ErrInvalidRange)
	}
	e := now
	if end != nil {
		e = *end
	}
	if aligned := floorEpoch(e, width); !aligned.Equal(e) {
		e = aligned.Add(width)
	}
	s := e.Add(-time.Duration(defaultBuckets) * width)
	if start != nil {
		s = floorEpoch(*start, width)
	}
	if !s.Before(e) {
		return span{}, fmt.Errorf("%w: start must be before end", ErrInvalidRange)
	}
	if n := e.Sub(s) / width; n > maxBuckets {
		return span{}, fmt.Errorf("%w: %d buckets exceed %d; widen window_seconds", ErrInvalidRange, n, maxBuckets)
	}
	return span{start: s.UTC(), end: e.UTC(), width: width}, nil
}

// floorEpoch rounds t down to a multiple of width since the Unix epoch, as
// date_bin does with origin to_timestamp(0).
func floorEpoch(t time.Time, width time.Duration) time.Time {
	epoch := time.Unix(0, 0).UTC()
	d := t.Sub(epoch)
	floored := d - d%width
	if d%width < 0 {
		floored -= width
	}
	return epoch.Add(floored)
}

// exactSpan resolves an optional range without alignment.
func exactSpan(start, end *time.Time, def time.Duration, now time.Time) (span, error) {
	e := now
	if end != nil {
		e = *end
	}
	s := e.Add(-def)
	if start != nil {
		s = *start
	}
	if !s.Before(e) {
		return span{}, fmt.Errorf("%w: start must be before end", ErrInvalidRange)
	}
	return span{start: s.UTC(), end: e.UTC()}, nil
}
