//go:build hostruntime

package snapshotter

import (
	"bytes"
	"context"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

// frozenDisk returns a directory on a filesystem of its own and a freeze
// that blocks every write to it until the test ends.
func frozenDisk(t *testing.T) (string, func()) {
	t.Helper()
	if os.Geteuid() != 0 {
		t.Fatal("a frozen filesystem needs root")
	}
	run := func(name string, args ...string) {
		t.Helper()
		if out, err := exec.CommandContext(context.Background(), name, args...).CombinedOutput(); err != nil {
			t.Fatalf("%s %v: %v: %s", name, args, err, out)
		}
	}
	image := filepath.Join(t.TempDir(), "disk")
	if err := os.WriteFile(image, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Truncate(image, 1<<30); err != nil {
		t.Fatal(err)
	}
	run("mkfs.ext4", "-q", image)
	dir := t.TempDir()
	run("mount", "-o", "loop", image, dir)
	t.Cleanup(func() { run("umount", dir) })
	return dir, func() {
		run("fsfreeze", "--freeze", dir)
		t.Cleanup(func() { run("fsfreeze", "--unfreeze", dir) })
	}
}

// A read never waits for the cache's disk: with every write to it blocked,
// a large object still reads whole and intact from the store, and none of
// its frames is stored yet.
func TestVolumeReadsNeverWaitForTheDisk(t *testing.T) {
	dir, freeze := frozenDisk(t)
	ts := newTestStore(t)
	up := &upstream{}
	cfg := testConfig("", up, slog.New(slog.DiscardHandler))
	cfg.CacheBytes = 1 << 30
	c, err := newFrameCache(t.Context(), filepath.Join(dir, "cache"), cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.writer.Wait)
	t.Cleanup(c.background.Wait)
	_, base := serveVolumes(t, ts, up, c)
	body := randomBytes(12, 12*volumeFrame)
	ts.put(t, "weights", body)
	freeze()
	read := make(chan []byte, 1)
	go func() {
		_, got := get(t, base+"weights", "")
		read <- got
	}()
	select {
	case got := <-read:
		if !bytes.Equal(got, body) {
			t.Fatal("the read came back changed")
		}
	case <-time.After(time.Minute):
		t.Fatal("the read waits for the frozen disk")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.frames) != 0 {
		t.Fatalf("%d frames stored on a frozen disk", len(c.frames))
	}
}
