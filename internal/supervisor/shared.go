package supervisor

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/runnerproto"
)

// sharedProcess is one runner process whose threads run the attempts of
// every slot (in_process). Each slot sends its invoke and waits for the
// outcome frame with its attempt id; one reader dispatches frames in order,
// so an attempt's output frames reach the outbox before its outcome.
type sharedProcess struct {
	p       *runnerProcess
	writeMu sync.Mutex

	mu      sync.Mutex
	waiting map[string]chan runnerproto.Frame
	// dead is closed once the reader stops: the process exited or broke
	// the protocol.
	dead    chan struct{}
	readErr error
}

// runShared runs the slots on one runner process, restarting it after it
// dies, until the slots drain or ctx ends. Killing the process to cancel
// one attempt ends the attempts of the other slots too; they report a
// crash, as they would if the process had died.
func (s *Supervisor) runShared(ctx context.Context, cfg *hostproto.Configure, slots []*slot, runs <-chan *hostproto.RunAttempt) error {
	// The process slot reads the runner's own stdout and stderr, which hold
	// output outside any attempt.
	process := &slot{sup: s, buf: make([]byte, readChunk), soleAttempt: func() string { return soleAttempt(slots) }}
	for {
		p, err := process.start(ctx, cfg)
		if err != nil {
			s.loadFailed(&hostproto.RunnerError{Type: "RunnerStartError", Message: err.Error()})
			return ErrLoadFailed
		}
		restart, err := s.serveShared(ctx, cfg, p, slots, runs)
		process.stop(p)
		if !restart || s.isDraining() {
			return err
		}
		s.log.Info("restarting shared runner", "exit", describeExit(p.waitErr))
		if !sleepCtx(ctx, time.Second) {
			return fmt.Errorf("restart shared runner: %w", ctx.Err())
		}
	}
}

func (s *Supervisor) serveShared(ctx context.Context, cfg *hostproto.Configure, p *runnerProcess, slots []*slot, runs <-chan *hostproto.RunAttempt) (bool, error) {
	stop := context.AfterFunc(ctx, p.terminate)
	defer stop()
	if loadErr := p.load(cfg, len(slots)); loadErr != nil {
		if ctx.Err() != nil {
			return false, fmt.Errorf("load handler: %w", ctx.Err())
		}
		s.loadFailed(loadErr)
		return false, ErrLoadFailed
	}
	shared := &sharedProcess{p: p, waiting: map[string]chan runnerproto.Frame{}, dead: make(chan struct{})}
	var reader sync.WaitGroup
	reader.Go(func() { shared.read(ctx, s) })
	defer reader.Wait()
	// A reader that stops ends the process, so the slots see it die.
	defer p.terminate()

	for _, sl := range slots {
		sl.outMu.Lock()
		sl.proc, sl.shared = p, true
		sl.outMu.Unlock()
		s.slotLoaded(sl)
	}
	g, gctx := errgroup.WithContext(ctx)
	restart := make(chan struct{}, len(slots))
	for _, sl := range slots {
		g.Go(func() error {
			again, err := sl.serveThread(gctx, shared, runs)
			if again {
				restart <- struct{}{}
			}
			return err
		})
	}
	err := g.Wait()
	for _, sl := range slots {
		sl.outMu.Lock()
		sl.proc = nil
		sl.outMu.Unlock()
	}
	if err != nil {
		return false, fmt.Errorf("serve shared slots: %w", err)
	}
	return len(restart) > 0, nil
}

// serveThread runs attempts on the shared process until it dies (restart),
// the slots drain or ctx ends.
func (sl *slot) serveThread(ctx context.Context, shared *sharedProcess, runs <-chan *hostproto.RunAttempt) (bool, error) {
	for {
		select {
		case <-ctx.Done():
			return false, fmt.Errorf("serve attempts: %w", ctx.Err())
		case <-shared.dead:
			return true, nil
		case run, ok := <-runs:
			if !ok {
				return false, nil
			}
			if !sl.sup.begin(sl, run.GetAttemptId()) {
				continue
			}
			finished, alive := shared.call(run)
			sl.finish(finished)
			if !alive {
				return true, nil
			}
		}
	}
}

// call runs one attempt and reports whether the process is still alive.
func (sp *sharedProcess) call(run *hostproto.RunAttempt) (*hostproto.AttemptFinished, bool) {
	attempt := run.GetAttemptId()
	invoke, err := invokeFrame(run)
	if err != nil {
		return crashed(attempt, "InvalidInput", err.Error()), true
	}
	reply := make(chan runnerproto.Frame, 1)
	sp.mu.Lock()
	sp.waiting[attempt] = reply
	sp.mu.Unlock()
	defer func() {
		sp.mu.Lock()
		delete(sp.waiting, attempt)
		sp.mu.Unlock()
	}()
	// Dependency frames apply to the next invoke, so they go out with it
	// under the write lock.
	sp.writeMu.Lock()
	err = sp.p.sendDependencies(run.GetDependencies())
	if err == nil {
		err = sp.p.send(invoke, run.GetInput())
	}
	sp.writeMu.Unlock()
	if err == nil {
		select {
		case frame := <-reply:
			if finished, err := attemptOutcome(attempt, frame); err == nil {
				return finished, true
			}
		case <-sp.dead:
		}
	}
	sp.p.terminate()
	<-sp.p.done
	<-sp.dead
	message := fmt.Sprintf("the shared runner exited during the attempt (%s)", describeExit(sp.p.waitErr))
	if sp.readErr != nil && !errors.Is(sp.readErr, errRunnerClosed) {
		message = fmt.Sprintf("runner protocol error: %v; %s", sp.readErr, message)
	}
	return crashed(attempt, "RunnerCrashed", message), false
}

// read dispatches frames until the process closes its socket: output goes
// to the outbox under its attempt, outcomes to the waiting slot.
func (sp *sharedProcess) read(ctx context.Context, s *Supervisor) {
	defer close(sp.dead)
	// held is output per attempt and stream that may begin a secret value
	// continued in the next frame. Only this goroutine touches it.
	type heldKey struct {
		attempt string
		stream  hostproto.LogStream
	}
	held := map[heldKey]string{}
	flushHeld := func(attempt string) {
		for _, stream := range []hostproto.LogStream{hostproto.LogStream_LOG_STREAM_STDOUT, hostproto.LogStream_LOG_STREAM_STDERR} {
			key := heldKey{attempt, stream}
			if text := held[key]; text != "" {
				s.out.push(outputMessage(attempt, stream, text))
			}
			delete(held, key)
		}
	}
	for {
		frame, err := sp.p.read()
		if err != nil {
			sp.readErr = err
			return
		}
		switch frame.Type {
		case runnerproto.FrameOutput:
			var out runnerproto.Output
			if err := frame.Decode(&out); err != nil {
				sp.readErr = err
				return
			}
			stream := hostproto.LogStream_LOG_STREAM_STDOUT
			if out.Stream == runnerproto.Stderr {
				stream = hostproto.LogStream_LOG_STREAM_STDERR
			}
			key := heldKey{out.AttemptId, stream}
			pending := held[key]
			text := s.redact.stream(&pending, strings.ToValidUTF8(string(frame.Payload), "\uFFFD"), false)
			held[key] = pending
			if text != "" {
				s.out.push(outputMessage(out.AttemptId, stream, text))
			}
			if s.out.waitOutputSpace(ctx) != nil {
				return
			}
		case runnerproto.FrameSucceeded, runnerproto.FrameFailed:
			var header struct {
				AttemptID string `json:"attempt_id"`
			}
			if err := frame.Decode(&header); err != nil {
				sp.readErr = err
				return
			}
			sp.mu.Lock()
			reply := sp.waiting[header.AttemptID]
			sp.mu.Unlock()
			if reply == nil {
				sp.readErr = fmt.Errorf("outcome for attempt %q that is not running", header.AttemptID)
				return
			}
			flushHeld(header.AttemptID)
			reply <- frame
		case runnerproto.FrameLoad, runnerproto.FrameLoaded, runnerproto.FrameLoadFailed, runnerproto.FrameDependency, runnerproto.FrameInvoke:
			sp.readErr = fmt.Errorf("unexpected %q frame from a shared runner", frame.Type)
			return
		default:
			sp.readErr = fmt.Errorf("unknown %q frame from a shared runner", frame.Type)
			return
		}
	}
}

// soleAttempt is the attempt the shared process runs when it runs exactly
// one. Output written to the process's own stdout and stderr, as by a
// subprocess or a C extension, belongs to it then; with several running it
// belongs to none.
func soleAttempt(slots []*slot) string {
	sole := ""
	for _, sl := range slots {
		sl.outMu.Lock()
		attempt, cancelled := sl.attempt, sl.cancelled
		sl.outMu.Unlock()
		if attempt == "" || cancelled {
			continue
		}
		if sole != "" {
			return ""
		}
		sole = attempt
	}
	return sole
}
