package diskengine

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// testStore is a fresh prefix in the local Garage bucket. Everything written
// under it is deleted when the test ends.
func testStore(t *testing.T) Store {
	t.Helper()
	cfg := storagetest.Config()
	store := Store{
		Endpoint: cfg.Endpoint, Region: cfg.Region, Bucket: cfg.Bucket, ForcePathStyle: true,
		Prefix: "test-diskengine/" + uuid.NewString() + "/",
		Credentials: func(context.Context) (Credentials, error) {
			return Credentials{AccessKeyID: cfg.AccessKeyID, SecretAccessKey: cfg.SecretAccessKey}, nil
		},
	}
	t.Cleanup(func() {
		objects, err := openStore(store)
		if err != nil {
			t.Error(err)
			return
		}
		ctx := context.Background()
		var keys []string
		if err := objects.list(ctx, store.Prefix, func(object storedObject) { keys = append(keys, object.Key) }); err != nil {
			t.Error(err)
			return
		}
		if err := objects.deleteKeys(ctx, keys); err != nil {
			t.Error(err)
		}
	})
	return store
}

func mustOpenStore(t *testing.T, store Store) *objectStore {
	t.Helper()
	objects, err := openStore(store)
	if err != nil {
		t.Fatal(err)
	}
	return objects
}

func requireTools(t *testing.T, tools ...string) {
	t.Helper()
	for _, tool := range tools {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skipf("%s is not installed", tool)
		}
	}
}

func run(t *testing.T, name string, args ...string) {
	t.Helper()
	if out, err := exec.CommandContext(t.Context(), name, args...).CombinedOutput(); err != nil {
		t.Fatalf("%s %v: %v: %s", name, args, err, out)
	}
}

// shortRoot is a root whose socket paths fit a unix socket, which a
// t.TempDir path does not.
func shortRoot(t *testing.T) string {
	t.Helper()
	root, err := os.MkdirTemp("", "de")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	return root
}

func testEngine(t *testing.T) *Engine {
	t.Helper()
	return New(shortRoot(t), slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug})))
}

func nbdURI(p diskPaths) string { return "nbd+unix:///" + exportName + "?socket=" + p.nbdSocket() }

// writeExport writes length bytes of pattern at offset through the running
// daemon's NBD export.
func writeExport(t *testing.T, p diskPaths, offset, length int64, pattern byte) {
	t.Helper()
	run(t, "qemu-io", "-f", "raw", "-c", fmt.Sprintf("write -P 0x%02x %d %d", pattern, offset, length), nbdURI(p))
}

// readExport reads the whole disk through the running daemon's NBD export.
func readExport(t *testing.T, p diskPaths) []byte {
	t.Helper()
	out := filepath.Join(t.TempDir(), "disk.raw")
	run(t, toolImage, "convert", "-f", "raw", "-O", "raw", nbdURI(p), out)
	data, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func requireSameDisk(t *testing.T, got, want []byte) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("disk is %d bytes, want %d", len(got), len(want))
	}
	if !bytes.Equal(got, want) {
		for i := range got {
			if got[i] != want[i] {
				t.Fatalf("disk differs first at byte %d: %#x, want %#x", i, got[i], want[i])
			}
		}
	}
}

// attachUnmounted prepares the disk as Attach does and starts its daemon, but
// connects no NBD device; the test reads and writes the daemon's export. The
// daemon is stopped when the test ends.
func attachUnmounted(t *testing.T, e *Engine, req AttachRequest) (diskPaths, *diskState, AttachResult) {
	t.Helper()
	p, err := e.paths(req.DiskID)
	if err != nil {
		t.Fatal(err)
	}
	state, err := loadState(p)
	if err != nil {
		t.Fatal(err)
	}
	state, result, err := e.prepare(t.Context(), p, state, req, mustOpenStore(t, req.Store))
	if err != nil {
		t.Fatal(err)
	}
	if err := startAttachment(t.Context(), p, state, req.Mountpoint); err != nil {
		t.Fatal(err)
	}
	pid := state.Attachment.DaemonPID
	t.Cleanup(func() {
		if err := stopDaemon(context.Background(), p, pid); err != nil {
			t.Error(err)
		}
	})
	return p, state, result
}

// sealUnmounted seals as Publish does, with nothing mounted to freeze.
func sealUnmounted(t *testing.T, p diskPaths, state *diskState) bool {
	t.Helper()
	client, err := dialQMP(t.Context(), p.qmpSocket())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = client.close() }()
	if err := reconcileHead(t.Context(), p, state, client); err != nil {
		t.Fatal(err)
	}
	sealed, err := sealFrozen(t.Context(), p, state, client)
	if err != nil {
		t.Fatal(err)
	}
	return sealed
}

func reload(t *testing.T, p diskPaths) *diskState {
	t.Helper()
	state, err := requireState(p)
	if err != nil {
		t.Fatal(err)
	}
	return state
}
