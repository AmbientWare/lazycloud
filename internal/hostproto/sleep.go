package hostproto

import (
	"context"
	"time"
)

// Sleep waits for d or ctx and reports whether ctx is still live.
func Sleep(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}
