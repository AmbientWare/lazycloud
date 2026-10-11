package diskengine

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"go.opentelemetry.io/otel/trace/noop"
	"golang.org/x/sys/unix"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
	"github.com/AmbientWare/lazycloud/internal/imagefs/snapshotter"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// testStore is a bucket of the test object store, deleted with everything
// in it when the test ends.
func testStore(t *testing.T) Store {
	t.Helper()
	cfg := storagetest.Config(t)
	return Store{
		Endpoint: cfg.Endpoint, Region: cfg.Region, Bucket: cfg.Bucket, ForcePathStyle: true,
		Credentials: func(context.Context) (Credentials, error) {
			return Credentials{AccessKeyID: cfg.AccessKeyID, SecretAccessKey: cfg.SecretAccessKey}, nil
		},
	}
}

// frameCount counts the disk's stored frames.
func frameCount(t *testing.T, store Store, diskID string) int {
	t.Helper()
	prefix := imagefs.DiskPrefix(diskID) + "frames/"
	out, err := storagetest.Client().ListObjectsV2(t.Context(), &s3.ListObjectsV2Input{Bucket: aws.String(store.Bucket), Prefix: aws.String(prefix)})
	if err != nil {
		t.Fatal(err)
	}
	return len(out.Contents)
}

// storeRemover deletes with the test store's own credentials, as the control
// plane does for a lease holder.
func storeRemover(store Store) Remover {
	return func(ctx context.Context, _ int64, keys []string, _ int64) error {
		for _, key := range keys {
			if _, err := storagetest.Client().DeleteObject(ctx, &s3.DeleteObjectInput{Bucket: &store.Bucket, Key: &key}); err != nil {
				return err
			}
		}
		return nil
	}
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

// shortRoot is a directory whose socket paths fit a unix socket, which a
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

// countingTransport counts the requests a snapshotter sends its store.
type countingTransport struct{ requests atomic.Int64 }

func (c *countingTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	c.requests.Add(1)
	return http.DefaultTransport.RoundTrip(r)
}

// host is a disk engine with its own snapshotter and frame cache, as one
// host runs them.
type host struct {
	engine *Engine
	bases  *layersource.Client
	store  *countingTransport
	// stop ends the snapshotter; its FUSE mounts go with it.
	stop func()
}

// newHost serves a snapshotter whose frame cache holds cacheBytes and
// returns an engine using it. Both stop when the test ends.
func newHost(t *testing.T, cacheBytes int64) *host {
	t.Helper()
	dir := shortRoot(t)
	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug}))
	counting := &countingTransport{}
	cfg := snapshotter.Config{
		Root: filepath.Join(dir, "sn"), CacheDir: filepath.Join(dir, "cache"), CacheBytes: cacheBytes, Fetches: 4,
		HTTP: &http.Client{Transport: counting}, Logger: logger, Tracer: noop.NewTracerProvider().Tracer(""),
	}
	socket := filepath.Join(dir, "sn.sock")
	ctx, cancel := context.WithCancel(context.Background())
	ready, served := make(chan struct{}), make(chan error, 1)
	go func() { served <- snapshotter.Serve(ctx, cfg, socket, func() { close(ready) }) }()
	select {
	case <-ready:
	case err := <-served:
		cancel()
		t.Fatal(err)
	}
	client, err := layersource.Dial(socket)
	if err != nil {
		t.Fatal(err)
	}
	stopped := false
	stop := func() {
		if stopped {
			return
		}
		stopped = true
		_ = client.Close()
		cancel()
		if err := <-served; err != nil {
			t.Error(err)
		}
		// The snapshotter leaves its mounts for the next start; a dead
		// FUSE mount must go before its directory can.
		_ = unix.Unmount(filepath.Join(cfg.Root, "disks"), unix.MNT_DETACH)
	}
	t.Cleanup(stop)
	return &host{engine: New(filepath.Join(dir, "d"), client, logger), bases: client, store: counting, stop: stop}
}

// grant gives the host's snapshotter the test store's credentials for disk.
func (h *host) grant(t *testing.T, store Store, diskID string) {
	t.Helper()
	creds, err := store.Credentials(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if err := h.bases.GrantDisk(t.Context(), diskID, &imagefsproto.DiskGrant{
		Endpoint: store.Endpoint, Region: store.Region, Bucket: store.Bucket, ForcePathStyle: store.ForcePathStyle,
		AccessKeyId: creds.AccessKeyID, SecretAccessKey: creds.SecretAccessKey, ExpiresAt: timestamppb.New(time.Now().Add(time.Hour)),
	}); err != nil {
		t.Fatal(err)
	}
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

// attachUnmounted attaches as Attach does but connects no NBD device; the
// test reads and writes the daemon's export. The daemon is stopped when the
// test ends.
func attachUnmounted(t *testing.T, h *host, store Store, req AttachRequest) (diskPaths, *diskState) {
	t.Helper()
	h.grant(t, store, req.DiskID)
	p, err := h.engine.paths(req.DiskID)
	if err != nil {
		t.Fatal(err)
	}
	state, err := loadState(p)
	if err != nil {
		t.Fatal(err)
	}
	state, err = h.engine.start(t.Context(), p, state, req, "/unused")
	if err != nil {
		t.Fatal(err)
	}
	pid := state.Attachment.DaemonPID
	t.Cleanup(func() {
		if err := stopDaemon(context.Background(), p, pid); err != nil {
			t.Error(err)
		}
	})
	return p, state
}

// sealUnmounted seals as Seal does, with nothing mounted to freeze.
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

// publishAndCommit publishes what is sealed and commits it as the control
// plane's record would.
func publishAndCommit(t *testing.T, e *Engine, diskID string, store Store, final bool) *Published {
	t.Helper()
	published, err := e.Publish(t.Context(), diskID, store, final)
	if err != nil {
		t.Fatal(err)
	}
	if published == nil {
		t.Fatal("nothing was published")
	}
	if err := e.CommitPublished(t.Context(), diskID, published.Generation, nil); err != nil {
		t.Fatal(err)
	}
	return published
}

func reload(t *testing.T, p diskPaths) *diskState {
	t.Helper()
	state, err := requireState(p)
	if err != nil {
		t.Fatal(err)
	}
	return state
}

func baseOf(p *Published) *Generation {
	return &Generation{Generation: p.Generation, IndexSHA256: p.IndexSHA256}
}
