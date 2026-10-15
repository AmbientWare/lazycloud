package snapshotter

import (
	"bytes"
	"context"
	"errors"
	"io/fs"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

// linkedCache is a frame cache of 64 MiB with a link tree.
func linkedCache(t *testing.T, up *upstream) *frameCache {
	t.Helper()
	cfg := testConfig("", up, slog.New(slog.DiscardHandler))
	cfg.CacheBytes, cfg.LinkDir = 64<<20, filepath.Join(t.TempDir(), "links")
	c, err := newFrameCache(t.Context(), t.TempDir(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.writer.Wait)
	t.Cleanup(c.background.Wait)
	return c
}

// link is where the link tree holds the test volume's object name.
func link(c *frameCache, v *volumes, ts *testStore, name string) string {
	workspace := v.buckets[ts.bucket].grant.workspace
	return filepath.Join(c.linkDir, workspace, strings.TrimSuffix(strings.TrimPrefix(ts.prefix, volumesPrefix), "/"), name)
}

// A volume object read whole is linked into the link tree at its volume
// path, with the store's modification time, so a workload opens it in
// place of the mount's file. Once the object changes in the store, the next
// read through the endpoint drops the link before the new version is
// linked, so no workload opens the old bytes as the new version. The
// least recently used object is evicted whole, link included.
func TestCompleteVolumeObjectsAreLinkedForWorkloads(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	up := &upstream{}
	c := linkedCache(t, up)
	v, base := serveVolumes(t, ts, up, c)
	old := randomBytes(21, 2*volumeFrame)
	ts.put(t, "weights", old)
	if resp, got := get(t, base+"weights", ""); resp.StatusCode != http.StatusOK || !bytes.Equal(got, old) {
		t.Fatalf("the read: %s", resp.Status)
	}
	path := link(c, v, ts, "weights")
	waitFor(t, func() bool { _, err := os.Stat(path); return err == nil })
	got, err := os.ReadFile(path) //nolint:gosec // the test's link
	if err != nil || !bytes.Equal(got, old) {
		t.Fatalf("the linked file: %v, intact %v", err, bytes.Equal(got, old))
	}
	head, err := ts.client.HeadObject(t.Context(), &s3.HeadObjectInput{Bucket: aws.String(ts.bucket), Key: aws.String(ts.prefix + "weights")})
	if err != nil {
		t.Fatal(err)
	}
	if info, _ := os.Stat(path); !info.ModTime().Equal(aws.ToTime(head.LastModified)) {
		t.Fatalf("the linked file is dated %v, the object %v", info.ModTime(), aws.ToTime(head.LastModified))
	} else if info.Mode().Perm()&0o044 != 0o044 {
		t.Fatalf("the linked file is %v; a workload of any user opens it", info.Mode().Perm())
	}

	changed := randomBytes(22, 2*volumeFrame)
	ts.put(t, "weights", changed)
	time.Sleep(observationLife)
	if resp, got := get(t, base+"weights", "0-0"); resp.StatusCode != http.StatusPartialContent || got[0] != changed[0] {
		t.Fatalf("the read after the change: %s", resp.Status)
	}
	if got, err := os.ReadFile(path); err == nil && bytes.Equal(got, old) { //nolint:gosec // the test's link
		t.Fatal("the old version stays linked after the object changed")
	}
	c.background.Wait()
	waitFor(t, func() bool { got, err := os.ReadFile(path); return err == nil && bytes.Equal(got, changed) }) //nolint:gosec // the test's link

	// Four more objects of 16 MiB overflow the 64 MiB bound: the oldest
	// goes whole.
	for _, name := range []string{"a", "b", "c", "d"} {
		ts.put(t, name, randomBytes(name[0], 2*volumeFrame))
		if resp, _ := get(t, base+name, ""); resp.StatusCode != http.StatusOK {
			t.Fatalf("read %s: %s", name, resp.Status)
		}
		c.background.Wait()
	}
	waitFor(t, func() bool { _, err := os.Stat(path); return err != nil })
	for _, name := range []string{"a", "b", "c", "d"} {
		waitFor(t, func() bool { _, err := os.Stat(link(c, v, ts, name)); return err == nil })
	}
	waitFor(t, func() bool { return cacheBytes(t, c.dir) <= c.limit })
}

// cacheBytes is the size of the files under a cache directory.
func cacheBytes(t *testing.T, dir string) int64 {
	t.Helper()
	var total int64
	err := filepath.WalkDir(dir, func(_ string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		total += info.Size()
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return total
}

// A peer serves a frame it holds to a host granted the workspace, which
// then fetches nothing from the store; it refuses an unsigned request and
// one of another workspace; and a peer that is gone costs the read only
// the fallback to the store.
func TestPeersServeCachedFramesToGrantedHosts(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	key := randomBytes(1, 32)
	peerUp := &upstream{}
	peer, _ := serveVolumes(t, ts, peerUp, linkedCache(t, peerUp))
	workspace := peer.buckets[ts.bucket].grant.workspace
	grant := testGrant(t, ts.bucket)
	grant.PeerKey = key
	if err := peer.grant(workspace, grant, testContainer, testKey, nil); err != nil {
		t.Fatal(err)
	}
	peerSrv := httptest.NewServer(peerServer{v: peer})
	t.Cleanup(peerSrv.Close)
	body := randomBytes(23, 3*volumeFrame)
	ts.put(t, "weights", body)
	// The peer reads the object, so it holds every frame.
	peerEndpoint := httptest.NewServer(peer)
	t.Cleanup(peerEndpoint.Close)
	if resp, got := get(t, peerEndpoint.URL+"/"+ts.bucket+"/"+ts.prefix+"weights", ""); resp.StatusCode != http.StatusOK || !bytes.Equal(got, body) {
		t.Fatalf("the peer's own read: %s", resp.Status)
	}
	peer.cache.background.Wait()
	waitFor(t, func() bool { _, err := os.Stat(link(peer.cache, peer, ts, "weights")); return err == nil })
	// A host granted the workspace with the peer named reads from it.
	readerUp := &upstream{}
	reader := newVolumes(linkedCache(t, readerUp), &http.Client{Transport: readerUp}, slog.New(slog.DiscardHandler))
	if err := reader.grant(workspace, grant, testContainer, testKey, []string{strings.TrimPrefix(peerSrv.URL, "http://")}); err != nil {
		t.Fatal(err)
	}
	readerSrv := httptest.NewServer(reader)
	t.Cleanup(readerSrv.Close)
	readerBase := readerSrv.URL + "/" + ts.bucket + "/" + ts.prefix
	if resp, got := get(t, readerBase+"weights", ""); resp.StatusCode != http.StatusOK || !bytes.Equal(got, body) {
		t.Fatalf("the read through the peer: %s", resp.Status)
	}
	if gets, _ := readerUp.counts(); gets != 0 {
		t.Fatalf("a read the peer held made %d store GETs", gets)
	}

	// Unsigned, signed with another key, or stale: refused.
	o, _ := peer.observed(ts.bucket, ts.prefix+"weights")
	req := peerRequest{workspace: workspace, bucket: ts.bucket, key: ts.prefix + "weights", etag: o.version.etag}
	for name, header := range map[string]string{"unsigned": "", "another key": req.sign(randomBytes(2, 32), time.Now()), "stale": req.sign(key, time.Now().Add(-2*peerSkew))} {
		r, err := http.NewRequestWithContext(t.Context(), http.MethodGet, peerSrv.URL+"/frame?"+req.query(), nil)
		if err != nil {
			t.Fatal(err)
		}
		r.Header.Set(peerHeader, header)
		resp, err := http.DefaultClient.Do(r)
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusForbidden {
			t.Fatalf("a peer request %s is %s", name, resp.Status)
		}
	}

	// A frame past the object's end: not held.
	past := req
	past.frame = 1000
	r, err := http.NewRequestWithContext(t.Context(), http.MethodGet, peerSrv.URL+"/frame?"+past.query(), nil)
	if err != nil {
		t.Fatal(err)
	}
	r.Header.Set(peerHeader, past.sign(key, time.Now()))
	resp, err := http.DefaultClient.Do(r)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("a frame past the object's end is %s", resp.Status)
	}

	// A peer that is gone: the read comes from the store.
	goneUp := &upstream{}
	gone := newVolumes(linkedCache(t, goneUp), &http.Client{Transport: goneUp}, slog.New(slog.DiscardHandler))
	if err := gone.grant(workspace, grant, testContainer, testKey, []string{"127.0.0.1:1"}); err != nil {
		t.Fatal(err)
	}
	goneSrv := httptest.NewServer(gone)
	t.Cleanup(goneSrv.Close)
	if resp, got := get(t, goneSrv.URL+"/"+ts.bucket+"/"+ts.prefix+"weights", ""); resp.StatusCode != http.StatusOK || !bytes.Equal(got, body) {
		t.Fatalf("the read past a gone peer: %s", resp.Status)
	}
	if gets, _ := goneUp.counts(); gets == 0 {
		t.Fatal("the read past a gone peer reached no store")
	}
	gone.mu.Lock()
	down := gone.peerDown["127.0.0.1:1"].After(time.Now())
	gone.mu.Unlock()
	if !down {
		t.Fatal("the gone peer is not left alone")
	}
}

// A cache start empties the link tree of files and keeps its directories:
// a container keeps its volume's directory bound while the snapshotter
// restarts under it.
func TestACacheStartKeepsTheLinkTreeDirectories(t *testing.T) {
	t.Parallel()
	dir := filepath.Join(t.TempDir(), "links")
	volume := filepath.Join(dir, "workspace", "volume")
	if err := os.MkdirAll(volume, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(volume, "model.bin"), []byte("old"), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := testConfig("", http.DefaultTransport, slog.New(slog.DiscardHandler))
	cfg.LinkDir = dir
	c, err := newFrameCache(t.Context(), t.TempDir(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.writer.Wait)
	if _, err := os.Stat(filepath.Join(volume, "model.bin")); err == nil {
		t.Fatal("a file of the last life stays in the link tree")
	}
	if info, err := os.Stat(volume); err != nil || !info.IsDir() {
		t.Fatalf("the volume's directory: %v", err)
	}
}

// storedSource is a test object of n frames of volumeFrame, linked nowhere.
type storedSource struct {
	name string
	n    int
}

func (s storedSource) key(frame int) frameKey { return frameKey{object: s.name, frame: frame} }
func (s storedSource) frameLen(int) int       { return volumeFrame }
func (s storedSource) fetch(context.Context, int) ([]byte, error) {
	return nil, errors.New("not fetched")
}
func (s storedSource) object() objectInfo { return objectInfo{size: int64(s.n) * volumeFrame} }

// An object being stored is never evicted to make room for its own frame:
// with the cache at its bound and two objects stored frame about, the
// older completes instead of both restarting forever.
func TestAnObjectUnderStoreIsNotItsOwnVictim(t *testing.T) {
	t.Parallel()
	c := linkedCache(t, &upstream{})
	c.limit = 20 << 20
	a, b := storedSource{name: "a", n: 2}, storedSource{name: "b", n: 2}
	data := make([]byte, volumeFrame)
	for _, step := range []struct {
		src   storedSource
		frame int
	}{{a, 0}, {b, 0}, {a, 1}} {
		if err := c.storeObjectFrame(step.src, step.src.key(step.frame), data); err != nil {
			t.Fatal(err)
		}
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if o := c.objects["a"]; o == nil || !o.complete() {
		t.Fatalf("object a after its second frame: %+v", o)
	}
	if c.used > c.limit {
		t.Fatalf("the cache holds %d bytes over its %d bound", c.used, c.limit)
	}
}
