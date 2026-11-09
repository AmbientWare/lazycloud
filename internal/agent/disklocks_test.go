package agent

import "testing"

// A disk's lock excludes a second caller until the first unlocks, and the
// lock table forgets a disk nobody holds or waits for.
func TestDiskLocksExcludeAndForget(t *testing.T) {
	t.Parallel()
	var l diskLocks
	unlock := l.lock("d1")
	acquired := make(chan func())
	go func() { acquired <- l.lock("d1") }()
	select {
	case <-acquired:
		t.Fatal("a second caller locked a held disk")
	default:
	}
	unlock()
	(<-acquired)()
	l.lock("d2")()
	if len(l.locks) != 0 {
		t.Fatalf("the table keeps %d disks nobody holds", len(l.locks))
	}
}
