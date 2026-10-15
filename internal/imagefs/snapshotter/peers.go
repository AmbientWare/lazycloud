package snapshotter

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Hosts of one region that hold a workspace's grant are its peers: before
// a frame is fetched from the store it is asked of each peer the start
// named, which answers from its own cache or not at all. A peer read is
// signed with the workspace's peer key, which only the grant carries, so a
// host serves frames of a workspace only to hosts granted it. A peer that
// fails is left alone for peerDownFor; every failure falls through to the
// store, so a peer costs a read at most peerTimeout.
const (
	peerTimeout = 3 * time.Second
	peerDownFor = time.Minute
	// peerSkew bounds how old a signed peer request may be.
	peerSkew   = 5 * time.Minute
	peerHeader = "X-Lazycloud-Peer"
)

// peerRequest names a frame of a version, as a peer request's query.
type peerRequest struct {
	workspace, bucket, key, etag string
	frame                        int
}

func (r peerRequest) query() string {
	return "workspace=" + r.workspace + "&bucket=" + r.bucket + "&key=" + s3Escape(r.key) + "&etag=" + s3Escape(r.etag) + "&frame=" + strconv.Itoa(r.frame)
}

// sign returns the request's signature header value at now: the time and
// the HMAC of the request with the workspace's peer key.
func (r peerRequest) sign(key []byte, now time.Time) string {
	at := strconv.FormatInt(now.Unix(), 10)
	mac := hmac.New(sha256.New, key)
	_, _ = io.WriteString(mac, r.query()+"\n"+at)
	return at + ":" + hex.EncodeToString(mac.Sum(nil))
}

// signed reports whether header signs r with key within peerSkew of now.
func (r peerRequest) signed(key []byte, header string, now time.Time) bool {
	at, _, ok := strings.Cut(header, ":")
	sent, err := strconv.ParseInt(at, 10, 64)
	if !ok || err != nil || now.Sub(time.Unix(sent, 0)).Abs() > peerSkew {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(header), []byte(r.sign(key, time.Unix(sent, 0)))) == 1
}

// fromPeers asks each live peer of the version's workspace for the frame
// and returns the first answer. False means no peer had it.
func (v *volumes) fromPeers(ctx context.Context, o objectVersion, frame int) ([]byte, bool) {
	g, ok := v.live(o.bucket)
	if !ok || len(g.peerKey) == 0 {
		return nil, false
	}
	v.mu.Lock()
	peers := make([]string, 0, len(v.peers[o.workspace]))
	for _, p := range v.peers[o.workspace] {
		if time.Now().After(v.peerDown[p]) {
			peers = append(peers, p)
		}
	}
	v.mu.Unlock()
	req := peerRequest{workspace: o.workspace, bucket: o.bucket, key: o.name, etag: o.etag, frame: frame}
	for _, p := range peers {
		data, err := v.askPeer(ctx, p, req, g.peerKey, o.frameLen(frame))
		if err == nil {
			return data, true
		}
		var status *peerStatus
		if !errors.As(err, &status) {
			v.mu.Lock()
			v.peerDown[p] = time.Now().Add(peerDownFor)
			v.mu.Unlock()
			v.log.Debug("a volume peer failed", "peer", p, "error", err)
		}
	}
	return nil, false
}

// peerStatus is a peer's answer other than the frame: 404 for a frame it
// does not hold.
type peerStatus struct{ code int }

func (s *peerStatus) Error() string { return "peer answered " + strconv.Itoa(s.code) }

func (v *volumes) askPeer(ctx context.Context, peer string, r peerRequest, key []byte, size int) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, peerTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+peer+"/frame?"+r.query(), nil)
	if err != nil {
		return nil, fmt.Errorf("ask a peer: %w", err)
	}
	req.Header.Set(peerHeader, r.sign(key, time.Now()))
	resp, err := v.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("ask a peer: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, &peerStatus{code: resp.StatusCode}
	}
	data := make([]byte, size)
	if _, err := io.ReadFull(resp.Body, data); err != nil {
		return nil, fmt.Errorf("read a peer's frame: %w", err)
	}
	return data, nil
}

// servePeers answers other hosts' frame requests from the cache: a frame
// of a granted workspace's object the host holds on disk or in memory.
type peerServer struct{ v *volumes }

func (s peerServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	frame, err := strconv.Atoi(q.Get("frame"))
	req := peerRequest{workspace: q.Get("workspace"), bucket: q.Get("bucket"), key: q.Get("key"), etag: q.Get("etag"), frame: frame}
	g, ok := s.v.live(req.bucket)
	if r.Method != http.MethodGet || r.URL.Path != "/frame" || err != nil || frame < 0 || !ok || g.workspace != req.workspace ||
		len(g.peerKey) == 0 || !req.signed(g.peerKey, r.Header.Get(peerHeader), time.Now()) {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	o, ok := s.v.observedVersion(req.bucket, req.key, req.etag)
	if !ok {
		http.Error(w, "unknown version", http.StatusNotFound)
		return
	}
	data := make([]byte, o.frameLen(frame))
	held, ok := s.v.cache.fromMemory(o.key(frame))
	if !ok {
		if !s.v.cache.storedFrame(o, frame, data, 0) {
			http.Error(w, "not cached", http.StatusNotFound)
			return
		}
		held = data
	}
	w.Header().Set("Content-Length", strconv.Itoa(len(held)))
	_, _ = w.Write(held)
}

// observedVersion returns the version of the object as etag names it when
// the cache holds frames of it: the observation or a stored object.
func (v *volumes) observedVersion(bucket, key, etag string) (objectVersion, bool) {
	v.mu.Lock()
	b := v.buckets[bucket]
	var o objectVersion
	if b != nil {
		if seen, ok := b.seen[key]; ok && seen.version.etag == etag {
			o = seen.version
		} else {
			o = objectVersion{v: v, workspace: b.grant.workspace, bucket: bucket, name: key, path: s3Escape(key), etag: etag}
		}
	}
	v.mu.Unlock()
	if b == nil {
		return objectVersion{}, false
	}
	if o.size == 0 {
		v.cache.mu.Lock()
		stored := v.cache.objects[o.key(0).object]
		if stored != nil {
			o.size, o.modified = stored.info.size, stored.info.modified
		}
		v.cache.mu.Unlock()
	}
	return o, o.size > 0
}
