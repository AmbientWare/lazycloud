package snapshotter

import (
	"bytes"
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
	waitFor(t, func() bool {
		c.mu.Lock()
		defer c.mu.Unlock()
		for _, o := range c.objects {
			if !o.linked {
				return false
			}
		}
		return len(c.objects) == 4
	})
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.used > c.limit || len(c.objects) != 4 {
		t.Fatalf("the cache holds %d bytes in %d objects after the eviction", c.used, len(c.objects))
	}
	for _, o := range c.objects {
		if !o.complete() || !o.linked {
			t.Fatalf("object %s is partial or unlinked after the eviction", o.key)
		}
	}
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

	waitFor(t, func() bool {
		peer.cache.mu.Lock()
		defer peer.cache.mu.Unlock()
		for _, o := range peer.cache.objects {
			return o.complete()
		}
		return false
	})
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
