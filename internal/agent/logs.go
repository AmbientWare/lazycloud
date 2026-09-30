package agent

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	logFlushInterval = 100 * time.Millisecond
	logBatchBytes    = 64 << 10
	// logBufferBytes bounds buffered and in-flight output per container.
	// Appends wait above it, which pushes back through the supervisor to the
	// runner instead of dropping output.
	logBufferBytes = 1 << 20
)

// logBatcher sends a container's output in ordered AppendLogs batches. A
// batch goes out after logFlushInterval, at logBatchBytes, or at once when a
// completion waits for it, so results never overtake their output.
type logBatcher struct {
	container string
	host      hostproto.HostServiceClient
	log       *slog.Logger

	mu      sync.Mutex
	lines   []*hostproto.LogLine
	bytes   int
	first   time.Time
	urgent  bool
	seq     uint64
	flushed uint64
	// changed is closed and replaced whenever bytes or flushed move.
	changed chan struct{}
	wake    chan struct{}
	done    chan struct{}
}

func newLogBatcher(container string, host hostproto.HostServiceClient, log *slog.Logger) *logBatcher {
	return &logBatcher{
		container: container, host: host, log: log,
		changed: make(chan struct{}), wake: make(chan struct{}, 1), done: make(chan struct{}),
	}
}

// append buffers a line, waiting while the buffer is full.
func (b *logBatcher) append(ctx context.Context, line *hostproto.LogLine) error {
	for {
		b.mu.Lock()
		if b.bytes < logBufferBytes {
			if len(b.lines) == 0 {
				b.first = time.Now()
			}
			b.lines = append(b.lines, line)
			b.bytes += len(line.GetData())
			b.seq++
			b.mu.Unlock()
			b.signal()
			return nil
		}
		changed := b.changed
		b.mu.Unlock()
		select {
		case <-changed:
		case <-b.done:
			return errStopped
		case <-ctx.Done():
			return fmt.Errorf("append log: %w", ctx.Err())
		}
	}
}

// mark returns the sequence number of the last appended line.
func (b *logBatcher) mark() uint64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.seq
}

// waitFlushed flushes at once and waits until every line up to seq was sent.
func (b *logBatcher) waitFlushed(ctx context.Context, seq uint64) {
	for {
		b.mu.Lock()
		if b.flushed >= seq {
			b.mu.Unlock()
			return
		}
		b.urgent = true
		changed := b.changed
		b.mu.Unlock()
		b.signal()
		select {
		case <-changed:
		case <-b.done:
			return
		case <-ctx.Done():
			return
		}
	}
}

func (b *logBatcher) signal() {
	select {
	case b.wake <- struct{}{}:
	default:
	}
}

// run sends batches until ctx ends.
func (b *logBatcher) run(ctx context.Context) {
	defer close(b.done)
	for {
		b.mu.Lock()
		pending, urgent, full, first := len(b.lines), b.urgent, b.bytes >= logBatchBytes, b.first
		b.mu.Unlock()
		if pending == 0 || (!urgent && !full && time.Since(first) < logFlushInterval) {
			wait := logFlushInterval - time.Since(first)
			if pending == 0 {
				wait = time.Hour
			}
			timer := time.NewTimer(wait)
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-b.wake:
			case <-timer.C:
			}
			timer.Stop()
			continue
		}
		if !b.flush(ctx) {
			return
		}
	}
}

// flush sends the oldest batch and reports whether the batcher should go on.
func (b *logBatcher) flush(ctx context.Context) bool {
	b.mu.Lock()
	var batch []*hostproto.LogLine
	size := 0
	for _, line := range b.lines {
		if len(batch) > 0 && size+len(line.GetData()) > logBatchBytes {
			break
		}
		batch = append(batch, line)
		size += len(line.GetData())
	}
	b.mu.Unlock()

	request := &hostproto.AppendLogsRequest{ContainerId: b.container, Lines: batch}
	delay := 100 * time.Millisecond
	for {
		_, err := b.host.AppendLogs(ctx, request)
		if err == nil {
			break
		}
		if ctx.Err() != nil {
			return false
		}
		if !retryable(err) {
			b.log.Error("dropping log batch", "container_id", b.container, "lines", len(batch), "error", err)
			break
		}
		if !sleep(ctx, delay) {
			return false
		}
		delay = min(2*delay, 5*time.Second)
	}

	b.mu.Lock()
	b.lines = b.lines[len(batch):]
	b.bytes -= size
	b.flushed += uint64(len(batch))
	if len(b.lines) == 0 {
		b.urgent = false
	} else {
		b.first = time.Now()
	}
	close(b.changed)
	b.changed = make(chan struct{})
	b.mu.Unlock()
	return true
}

// retryable reports whether a call may succeed if repeated.
func retryable(err error) bool {
	code := status.Code(err)
	return code == codes.Unavailable || code == codes.DeadlineExceeded || code == codes.ResourceExhausted || code == codes.Aborted
}
