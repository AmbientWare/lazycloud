package agent

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	logFlushInterval = 100 * time.Millisecond
	logBatchBytes    = 64 << 10
	// logBufferBytes bounds buffered and in-flight output per container.
	// Appends wait above it, which pushes back on the output's source, the
	// runner through the supervisor or the builder's output stream, instead
	// of dropping output.
	logBufferBytes = 1 << 20
)

// logBatcher sends one container's output through send in ordered batches.
// A batch goes out logFlushInterval after its first line, at logBatchBytes,
// or at once when a completion waits for it, so results never overtake
// their output.
type logBatcher[L any] struct {
	size func(L) int
	send func(context.Context, []L) error
	log  *slog.Logger

	mu      sync.Mutex
	lines   []L
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

func newLogBatcher[L any](size func(L) int, send func(context.Context, []L) error, log *slog.Logger) *logBatcher[L] {
	return &logBatcher[L]{
		size: size, send: send, log: log,
		changed: make(chan struct{}), wake: make(chan struct{}, 1), done: make(chan struct{}),
	}
}

// containerLogs sends a workload container's output as AppendLogs batches.
func containerLogs(host hostproto.HostServiceClient, container string, log *slog.Logger) *logBatcher[*hostproto.LogLine] {
	return newLogBatcher(func(l *hostproto.LogLine) int { return len(l.GetData()) },
		func(ctx context.Context, lines []*hostproto.LogLine) error {
			_, err := host.AppendLogs(ctx, &hostproto.AppendLogsRequest{ContainerId: container, Lines: lines})
			return err //nolint:wrapcheck // The batcher reads the call's status.
		}, log)
}

// buildLogs sends a build container's output as AppendImageBuildLogs
// batches.
type buildLogs struct {
	*logBatcher[*hostproto.BuildLogLine]
}

func newBuildLogs(host hostproto.HostServiceClient, container string, log *slog.Logger) buildLogs {
	return buildLogs{newLogBatcher(func(l *hostproto.BuildLogLine) int { return len(l.GetData()) },
		func(ctx context.Context, lines []*hostproto.BuildLogLine) error {
			_, err := host.AppendImageBuildLogs(ctx, &hostproto.AppendImageBuildLogsRequest{ContainerId: container, Lines: lines})
			return err //nolint:wrapcheck // The batcher reads the call's status.
		}, log)}
}

// add queues a line of build output stamped now.
func (b buildLogs) add(ctx context.Context, line string) {
	b.append(ctx, &hostproto.BuildLogLine{Data: line, Time: timestamppb.Now()})
}

// append buffers a line, waiting while the buffer is full. The line is
// dropped if ctx ends or the batcher stops first.
func (b *logBatcher[L]) append(ctx context.Context, line L) {
	for {
		b.mu.Lock()
		if b.bytes < logBufferBytes {
			if len(b.lines) == 0 {
				b.first = time.Now()
			}
			b.lines = append(b.lines, line)
			b.bytes += b.size(line)
			b.seq++
			b.mu.Unlock()
			b.signal()
			return
		}
		changed := b.changed
		b.mu.Unlock()
		select {
		case <-changed:
		case <-b.done:
			return
		case <-ctx.Done():
			return
		}
	}
}

// mark returns the sequence number of the last appended line.
func (b *logBatcher[L]) mark() uint64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.seq
}

// waitFlushed flushes at once and waits until every line up to seq was sent.
func (b *logBatcher[L]) waitFlushed(ctx context.Context, seq uint64) {
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

func (b *logBatcher[L]) signal() {
	select {
	case b.wake <- struct{}{}:
	default:
	}
}

// run sends batches until ctx ends.
func (b *logBatcher[L]) run(ctx context.Context) {
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
func (b *logBatcher[L]) flush(ctx context.Context) bool {
	b.mu.Lock()
	var batch []L
	size := 0
	for _, line := range b.lines {
		if len(batch) > 0 && size+b.size(line) > logBatchBytes {
			break
		}
		batch = append(batch, line)
		size += b.size(line)
	}
	b.mu.Unlock()

	delay := 100 * time.Millisecond
	for {
		err := b.send(ctx, batch)
		if err == nil {
			break
		}
		if ctx.Err() != nil {
			return false
		}
		if !retryable(err) {
			b.log.Error("dropping log batch", "lines", len(batch), "error", err)
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
