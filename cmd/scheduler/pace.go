package main

import (
	"context"
	"log/slog"
	"sync"
	"time"
)

const (
	// safetyTick bounds how long a quiet loop on the leader waits without a
	// wake, covering a lost notification.
	safetyTick = 30 * time.Second
	// leaderCheck paces the leader's ping of the session that holds its lock.
	leaderCheck = 10 * time.Second
)

// pace decides when passes run without a wake. Every replica runs a pass
// when a NOTIFY wakes it or its lock was held, but only the leader runs
// passes on a timer, so idle work does not grow with replicas. While the
// leader finds no live work (no queued or running task and no live
// container), quiet loops wait for a wake or the safety tick instead of their
// interval.
type pace struct {
	mu      sync.Mutex
	leading bool
	live    bool
	// changed closes when leading or live changes.
	changed chan struct{}
}

func newPace() *pace {
	return &pace{live: true, changed: make(chan struct{})}
}

func (p *pace) state() (leading, live bool, changed <-chan struct{}) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.leading, p.live, p.changed
}

func (p *pace) set(update func()) {
	p.mu.Lock()
	defer p.mu.Unlock()
	leading, live := p.leading, p.live
	update()
	if leading != p.leading || live != p.live {
		close(p.changed)
		p.changed = make(chan struct{})
	}
}

func (p *pace) setLeading(leading bool) { p.set(func() { p.leading = leading }) }
func (p *pace) setLive(live bool)       { p.set(func() { p.live = live }) }

// isLeading reports whether this replica runs timed passes.
func (p *pace) isLeading() bool {
	leading, _, _ := p.state()
	return leading
}

// watch keeps live current on the leader: after every wake, every tick while
// live, and every safety tick while idle. A probe failure counts as live, so
// errors never quiet the loops.
func (p *pace) watch(ctx context.Context, probe func(context.Context) (bool, error), wake <-chan struct{}, logger *slog.Logger) error {
	for {
		leading, _, _ := p.state()
		if leading {
			live, err := probe(ctx)
			if err != nil {
				if ctx.Err() != nil {
					return nil
				}
				logger.ErrorContext(ctx, "live work probe", "error", err)
				live = true
			}
			p.setLive(live)
		}
		leading, live, changed := p.state()
		var timer *time.Timer
		var fire <-chan time.Time
		if leading {
			wait := safetyTick
			if live {
				wait = tick
			}
			timer = time.NewTimer(wait)
			fire = timer.C
		}
		select {
		case <-ctx.Done():
		case <-fire:
		case <-wake:
		case <-changed:
		}
		if timer != nil {
			timer.Stop()
		}
		if ctx.Err() != nil {
			return nil
		}
	}
}

// cadence is when a loop runs without a wake on the leader.
type cadence struct {
	every time.Duration
	// quiet waits for a wake or the safety tick while there is no live work.
	quiet bool
	// due, if set, reports when time alone next makes a quiet loop's work
	// due, such as a schedule's next occurrence.
	due func(context.Context) (time.Time, bool, error)
}

// loop runs pass now, then after every wake, contended retry, change of
// leadership or live work, and on the leader after the cadence's wait, until
// ctx ends. pass reports whether it found its lock held or work left.
// Failures are logged by pass and retried on the next wake or tick; a nil wake
// channel is never ready.
func (p *pace) loop(ctx context.Context, c cadence, wake, alsoWake <-chan struct{}, pass func(context.Context) bool) error {
	for {
		again := pass(ctx)
		// Taken before the wait is chosen, so a change in between still
		// wakes the loop.
		_, _, changed := p.state()
		wait, ok := p.wait(ctx, c, again)
		var timer *time.Timer
		var fire <-chan time.Time
		if ok {
			timer = time.NewTimer(wait)
			fire = timer.C
		}
		select {
		case <-ctx.Done():
		case <-fire:
		case <-wake:
		case <-alsoWake:
		case <-changed:
		}
		if timer != nil {
			timer.Stop()
		}
		if ctx.Err() != nil {
			return nil
		}
	}
}

// wait is how long the loop sleeps without a wake; false means until one.
func (p *pace) wait(ctx context.Context, c cadence, again bool) (time.Duration, bool) {
	if again {
		return contendedRetry, true
	}
	leading, live, _ := p.state()
	switch {
	case !leading:
		return 0, false
	case !c.quiet || live:
		return c.every, true
	case c.due != nil:
		at, ok, err := c.due(ctx)
		if err != nil {
			return c.every, true
		}
		if ok {
			return min(max(time.Until(at), 0), safetyTick), true
		}
	}
	return safetyTick, true
}
