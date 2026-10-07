package main

import (
	"context"
	"sync"
	"testing"
	"time"
)

// A pass that asks to run again runs then on a replica that does not lead,
// without a wake, and only once.
func TestAPassThatAsksToRunAgainRerunsOnAnyReplica(t *testing.T) {
	p := newPace()
	ctx, cancel := context.WithCancel(t.Context())
	runs := make(chan struct{}, 3)
	// The loop's goroutine alone reads and writes them.
	var at time.Time
	passes := 0
	var wg sync.WaitGroup
	wg.Go(func() {
		rerun := func() (time.Time, bool) { return at, !at.IsZero() }
		if err := p.loop(ctx, cadence{every: time.Hour, rerun: rerun}, nil, nil, func(context.Context) bool {
			passes++
			at = time.Time{}
			if passes == 1 {
				at = time.Now().Add(50 * time.Millisecond)
			}
			runs <- struct{}{}
			return false
		}); err != nil {
			t.Error(err)
		}
	})
	defer wg.Wait()
	defer cancel()
	for n := range 2 {
		select {
		case <-runs:
		case <-time.After(2 * time.Second):
			t.Fatalf("pass %d did not run", n+1)
		}
	}
	select {
	case <-runs:
		t.Fatal("the loop ran again without a request or a wake")
	case <-time.After(200 * time.Millisecond):
	}
}
