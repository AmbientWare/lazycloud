package agent

import (
	"runtime"
	"testing"
)

// A disk's lock excludes a waiting caller until the holder unlocks, and the
// lock table forgets a disk nobody holds or waits for.
func TestDiskLocksExcludeAndForget(t *testing.T) {
	t.Parallel()
	var l diskLocks
	unlock := l.lock("d1")
	acquired := make(chan func())
	go func() { acquired <- l.lock("d1") }()
	for waiting := false; !waiting; runtime.Gosched() {
		l.mu.Lock()
		waiting = l.locks["d1"].users == 2
		l.mu.Unlock()
	}
	select {
	case <-acquired:
		t.Fatal("a second caller locked a held disk")
	default:
	}
	unlock()
	(<-acquired)()
	if len(l.locks) != 0 {
		t.Fatalf("the table keeps %d disks nobody holds", len(l.locks))
	}
}
