package snapshotter

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/hex"
	"encoding/xml"
	"fmt"
	"io"
	"iter"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	v4 "github.com/aws/aws-sdk-go-v2/aws/signer/v4"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	"golang.org/x/sync/singleflight"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
)

const (
	// volumesPrefix holds every key the endpoint reaches; a workspace's
	// grant reaches its disks too.
	volumesPrefix = "volumes/"
	// volumeFrame is the part of an object one fetch reads and the cache
	// keeps as one frame.
	volumeFrame = 8 << 20
	// minCachedObject is the smallest object reads take from the cache;
	// smaller ones are forwarded.
	minCachedObject = 1 << 20
	// observationLife is how long a HEAD's answer names an object's
	// version, as GeeseFS's --stat-cache-ttl.
	observationLife = time.Second
	// maxDeleteBody bounds a multi-object delete, 1000 keys.
	maxDeleteBody   = 1 << 20
	unsignedPayload = "UNSIGNED-PAYLOAD"
)

// volumes serves the S3 endpoint of the host's platform volumes: it
// forwards requests for keys under volumes/ to each bucket's store, signed
// with the bucket's grant, and answers plain GETs of large objects from the
// frame cache. The store is the cache's durable source.
type volumes struct {
	cache  *frameCache
	http   *http.Client
	log    *slog.Logger
	proxy  *httputil.ReverseProxy
	signer *v4.Signer
	// addr is the endpoint's address, empty on a host that does not cache
	// volumes; set before the first call.
	addr string
	// heads makes concurrent reads of one object share one HEAD.
	heads singleflight.Group

	mu sync.Mutex
	// buckets holds each granted bucket by name.
	buckets map[string]*bucket
	// filling names the versions being fetched whole.
	filling map[string]bool
}

// bucket is one workspace's granted bucket: its grant, the key requests
// for it name, and the versions HEADs reported, each kept only while no
// write of its key, or of the bucket, ended after the HEAD was sent.
// Guarded by volumes.mu.
type bucket struct {
	grant   volumeGrant
	key     string
	seen    map[string]observation
	written map[string]time.Time
	cleared time.Time
}

// volumeGrant is where a bucket's store is and the credential requests to
// it are signed with.
type volumeGrant struct {
	workspace, name string
	endpoint        *url.URL
	region          string
	pathStyle       bool
	creds           aws.Credentials
	objects         imagefs.Bucket
}

// observation is an object's version as a HEAD sent at a time reported
// it, and the headers a GET of it answers with: ETag, Last-Modified,
// Content-Type and the x-amz-meta- ones.
type observation struct {
	version objectVersion
	header  http.Header
	at      time.Time
}

func newVolumes(cache *frameCache, client *http.Client, log *slog.Logger) *volumes {
	v := &volumes{
		cache: cache, http: client, log: log,
		signer:  v4.NewSigner(func(o *v4.SignerOptions) { o.DisableURIPathEscaping = true }),
		buckets: map[string]*bucket{}, filling: map[string]bool{},
	}
	// Forwarding has no deadline: a server-side copy of gigabytes runs for
	// minutes.
	v.proxy = &httputil.ReverseProxy{
		Rewrite:        v.rewrite,
		Transport:      client.Transport,
		ModifyResponse: v.observe,
		ErrorLog:       slog.NewLogLogger(log.Handler(), slog.LevelWarn),
	}
	return v
}

// grant records a workspace bucket's grant, unless the current one expires
// later, and the key its requests name, and forgets buckets whose grants
// expired.
func (v *volumes) grant(workspace, key string, g *imagefsproto.BucketGrant) error {
	if v.addr == "" {
		return status.Error(codes.FailedPrecondition, "this host does not cache volumes")
	}
	if _, err := uuid.Parse(workspace); err != nil || len(key) < 16 {
		return status.Error(codes.InvalidArgument, "a volume grant needs a workspace id and a key of 16 bytes or more")
	}
	creds, err := grantCredentials(g)
	if err != nil {
		return err
	}
	endpoint, err := url.Parse(g.GetEndpoint())
	if err != nil || endpoint.Host == "" || (endpoint.Scheme != "https" && endpoint.Scheme != "http") {
		return status.Error(codes.InvalidArgument, "a volume grant needs an absolute HTTP endpoint")
	}
	vg := volumeGrant{
		workspace: workspace, name: g.GetBucket(), endpoint: endpoint, region: g.GetRegion(), pathStyle: g.GetForcePathStyle(), creds: creds,
		objects: grantBucket(v.http, g, creds),
	}
	v.mu.Lock()
	defer v.mu.Unlock()
	for name, b := range v.buckets {
		if !time.Now().Before(b.grant.creds.Expires) {
			delete(v.buckets, name)
		}
	}
	b := v.buckets[vg.name]
	if b == nil {
		b = &bucket{seen: map[string]observation{}, written: map[string]time.Time{}}
		v.buckets[vg.name] = b
	}
	if b.key != key || !b.grant.creds.Expires.After(creds.Expires) {
		b.grant, b.key = vg, key
	}
	return nil
}

// grantBucket reads the objects g grants, one attempt per call: the cache
// retries.
func grantBucket(client *http.Client, g *imagefsproto.BucketGrant, creds aws.Credentials) imagefs.Bucket {
	return imagefs.NewBucket(g.GetEndpoint(), g.GetRegion(), g.GetBucket(), g.GetForcePathStyle(), credentials.StaticCredentialsProvider{Value: creds},
		func(o *s3.Options) { o.HTTPClient, o.RetryMaxAttempts = client, 1 })
}

// grantCredentials checks a bucket grant and returns its credential.
func grantCredentials(g *imagefsproto.BucketGrant) (aws.Credentials, error) {
	if g.GetEndpoint() == "" || g.GetRegion() == "" || g.GetBucket() == "" || g.GetAccessKeyId() == "" || g.GetExpiresAt() == nil {
		return aws.Credentials{}, status.Error(codes.InvalidArgument, "a bucket grant needs an endpoint, a region, a bucket, a key and an expiry")
	}
	return aws.Credentials{
		AccessKeyID: g.GetAccessKeyId(), SecretAccessKey: g.GetSecretAccessKey(), SessionToken: g.GetSessionToken(),
		CanExpire: true, Expires: g.GetExpiresAt().AsTime(),
	}, nil
}

// granted returns the bucket's grant while it lasts and the request names
// its key as its signature's access key.
func (v *volumes) granted(name string, r *http.Request) (volumeGrant, bool) {
	_, credential, _ := strings.Cut(r.Header.Get("Authorization"), "Credential=")
	key, _, _ := strings.Cut(credential, "/")
	v.mu.Lock()
	defer v.mu.Unlock()
	b := v.buckets[name]
	if b == nil || !time.Now().Before(b.grant.creds.Expires) || subtle.ConstantTimeCompare([]byte(key), []byte(b.key)) != 1 {
		return volumeGrant{}, false
	}
	return b.grant, true
}

// forwarded is what a forwarded request carries to rewrite and observe.
type forwarded struct {
	grant volumeGrant
	// key is the object's, empty for a bucket request; path is its
	// s3Escape form with its leading slash.
	key, path string
	sent      time.Time
}

type forwardedKey struct{}

// ServeHTTP answers a path-style S3 request for a granted bucket that
// names its key.
func (v *volumes) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	name, path, _ := strings.Cut(strings.TrimPrefix(r.URL.EscapedPath(), "/"), "/")
	g, ok := v.granted(name, r)
	key, err := url.PathUnescape(path)
	if !ok || err != nil || !inVolumes(r, name, key) {
		s3Error(w, http.StatusForbidden, "AccessDenied")
		return
	}
	f := forwarded{grant: g, key: key, path: "/" + s3Escape(key), sent: time.Now()}
	q := r.URL.Query()
	switch {
	case r.Method == http.MethodGet && key != "" && r.URL.RawQuery == "" && plainGet(r.Header):
		if v.serveCached(w, r, f) {
			return
		}
	case r.Method == http.MethodPut && !q.Has("uploadId"), r.Method == http.MethodDelete && !q.Has("uploadId"),
		r.Method == http.MethodPost && q.Has("uploadId"):
		defer v.wrote(name, key)
	case r.Method == http.MethodPost && key == "":
		defer v.wrote(name, "")
	}
	v.proxy.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), forwardedKey{}, f)))
}

// inVolumes reports whether r reaches only keys under volumes/ of its
// bucket: an object's, its copy source's, a listing's prefix or a
// multi-object delete's keys. A bucket's HEAD reaches none.
func inVolumes(r *http.Request, name, key string) bool {
	if key != "" {
		if source := r.Header.Get("X-Amz-Copy-Source"); source != "" {
			source, _, _ = strings.Cut(source, "?")
			source, _ = url.PathUnescape(strings.TrimPrefix(source, "/"))
			from, sourceKey, _ := strings.Cut(source, "/")
			if from != name || !strings.HasPrefix(sourceKey, volumesPrefix) {
				return false
			}
		}
		return strings.HasPrefix(key, volumesPrefix)
	}
	q := r.URL.Query()
	switch {
	case r.Method == http.MethodHead && r.URL.RawQuery == "":
		return true
	case r.Method == http.MethodGet:
		return strings.HasPrefix(q.Get("prefix"), volumesPrefix)
	case r.Method == http.MethodPost && q.Has("delete"):
		body, err := io.ReadAll(io.LimitReader(r.Body, maxDeleteBody+1))
		if err != nil || len(body) > maxDeleteBody {
			return false
		}
		r.Body = io.NopCloser(bytes.NewReader(body))
		var doc struct {
			Keys []string `xml:"Object>Key"`
		}
		if xml.Unmarshal(body, &doc) != nil {
			return false
		}
		for _, k := range doc.Keys {
			if !strings.HasPrefix(k, volumesPrefix) {
				return false
			}
		}
		return true
	}
	return false
}

// plainGet reports a GET the cache can answer: no conditions, and a range
// of the form bytes=first-last or none.
func plainGet(h http.Header) bool {
	for _, name := range []string{"If-Match", "If-None-Match", "If-Modified-Since", "If-Unmodified-Since"} {
		if h.Get(name) != "" {
			return false
		}
	}
	_, _, ok := byteRange(h.Get("Range"), 1<<62)
	return ok
}

// rewrite points a forwarded request at its bucket's store and signs it.
func (v *volumes) rewrite(pr *httputil.ProxyRequest) {
	f := pr.In.Context().Value(forwardedKey{}).(forwarded) //nolint:forcetypeassert // ServeHTTP sets it
	pr.Out.URL = f.grant.url(f.path, pr.In.URL.RawQuery)
	pr.Out.Host = ""
	if err := v.sign(pr.Out, f.grant); err != nil {
		v.log.Warn("signing a volume request failed", "bucket", f.grant.name, "error", err)
	}
}

// s3Escape escapes key as SigV4 canonical paths do: every byte but
// unreserved ones and "/".
func s3Escape(key string) string {
	var b strings.Builder
	for i := range len(key) {
		switch c := key[i]; {
		case 'A' <= c && c <= 'Z', 'a' <= c && c <= 'z', '0' <= c && c <= '9', strings.IndexByte("-_.~/", c) >= 0:
			b.WriteByte(c)
		default:
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// url is where path, an escaped object path with its leading slash, is
// in the grant's store.
func (g volumeGrant) url(path, query string) *url.URL {
	u := *g.endpoint
	raw := strings.TrimSuffix(u.EscapedPath(), "/")
	if g.pathStyle {
		raw += "/" + g.name
	} else {
		u.Host = g.name + "." + u.Host
	}
	raw += path
	u.Path, _ = url.PathUnescape(raw)
	u.RawPath, u.RawQuery = raw, query
	return &u
}

// sign replaces the client's signature with the grant's, keeping the
// payload hash the client signed when it sent one.
func (v *volumes) sign(r *http.Request, g volumeGrant) error {
	payload := r.Header.Get("X-Amz-Content-Sha256")
	if raw, err := hex.DecodeString(payload); err != nil || len(raw) != 32 {
		payload = unsignedPayload
	}
	for _, h := range []string{"Authorization", "X-Amz-Date", "X-Amz-Security-Token"} {
		r.Header.Del(h)
	}
	r.Header.Set("X-Amz-Content-Sha256", payload)
	if g.creds.SessionToken != "" {
		r.Header.Set("X-Amz-Security-Token", g.creds.SessionToken)
	}
	return v.signer.SignHTTP(r.Context(), g.creds, r, payload, "s3", g.region, time.Now()) //nolint:wrapcheck // callers name the request
}

// observe records the version a HEAD of an object reports, unless a write
// of it ended since the HEAD was sent.
func (v *volumes) observe(resp *http.Response) error {
	f, _ := resp.Request.Context().Value(forwardedKey{}).(forwarded)
	etag := resp.Header.Get("Etag")
	if f.key == "" || resp.Request.Method != http.MethodHead || resp.StatusCode != http.StatusOK || etag == "" || resp.ContentLength < 0 {
		return nil
	}
	header := http.Header{}
	for name, values := range resp.Header {
		if strings.HasPrefix(name, "X-Amz-Meta-") || name == "Etag" || name == "Last-Modified" || name == "Content-Type" {
			header[name] = values
		}
	}
	v.mu.Lock()
	defer v.mu.Unlock()
	b := v.buckets[f.grant.name]
	if b == nil || !f.sent.After(b.cleared) || !f.sent.After(b.written[f.key]) {
		return nil
	}
	ver := objectVersion{v: v, workspace: f.grant.workspace, bucket: f.grant.name, name: f.key, etag: etag, size: resp.ContentLength}
	b.seen[f.key] = observation{version: ver, header: header, at: f.sent}
	return nil
}

// wrote records that a write of key, or with "" of any key, ended, and
// forgets the observations it outdates. A mark outlives any HEAD sent
// before it, which fetchTimeout bounds.
func (v *volumes) wrote(name, key string) {
	v.mu.Lock()
	defer v.mu.Unlock()
	b := v.buckets[name]
	if b == nil {
		return
	}
	now := time.Now()
	if key == "" {
		b.cleared = now
		clear(b.seen)
		return
	}
	b.written[key] = now
	delete(b.seen, key)
	for k, at := range b.written {
		if now.Sub(at) > fetchTimeout {
			delete(b.written, k)
		}
	}
}

// version returns the object's version, observed within observationLife
// or asked of the store with a HEAD. False means the store named none, or
// could not be asked, and the GET is forwarded.
func (v *volumes) version(ctx context.Context, f forwarded) (observation, bool) {
	if o, ok := v.observed(f.grant.name, f.key); ok {
		return o, true
	}
	_, err, _ := v.heads.Do(f.grant.name+"/"+f.key, func() (any, error) {
		f.sent = time.Now()
		ctx, cancel := context.WithTimeout(context.WithValue(context.WithoutCancel(ctx), forwardedKey{}, f), fetchTimeout)
		defer cancel()
		req, err := http.NewRequestWithContext(ctx, http.MethodHead, f.grant.url(f.path, "").String(), nil)
		if err != nil {
			return nil, fmt.Errorf("ask an object's version: %w", err)
		}
		if err := v.sign(req, f.grant); err != nil {
			return nil, fmt.Errorf("ask an object's version: %w", err)
		}
		resp, err := v.http.Do(req)
		if err != nil {
			return nil, fmt.Errorf("ask an object's version: %w", err)
		}
		_ = resp.Body.Close()
		return nil, v.observe(resp)
	})
	if err != nil {
		v.log.Warn("asking a volume object's version failed", "bucket", f.grant.name, "key", f.key, "error", err)
		return observation{}, false
	}
	return v.observed(f.grant.name, f.key)
}

func (v *volumes) observed(name, key string) (observation, bool) {
	v.mu.Lock()
	defer v.mu.Unlock()
	b := v.buckets[name]
	if b == nil {
		return observation{}, false
	}
	o, ok := b.seen[key]
	return o, ok && time.Since(o.at) <= observationLife
}

// serveCached answers a GET of a large object from the cache and reports
// whether it did; a GET it does not answer is forwarded. A read the cache
// misses of an object within a quarter of the cache fetches the whole
// object in the background. A frame the store refuses fails the GET as
// 503, which GeeseFS retries, or once the answer began, cuts it short.
func (v *volumes) serveCached(w http.ResponseWriter, r *http.Request, f forwarded) bool {
	o, ok := v.version(r.Context(), f)
	if !ok || o.version.size < minCachedObject {
		return false
	}
	ver := o.version
	first, last, ok := byteRange(r.Header.Get("Range"), ver.size)
	if !ok {
		return false
	}
	if ver.size <= v.cache.limit/4 && !v.cache.cached(ver.key(int(first/volumeFrame))) {
		v.fill(ver)
	}
	buf := make([]byte, min(maxRead, last-first+1))
	for pos, started := first, false; pos <= last; {
		frame, within := int(pos/volumeFrame), pos%volumeFrame
		p := buf[:min(int64(len(buf)), volumeFrame-within, last-pos+1)]
		if _, _, err := v.cache.read(ver, frame, p, within); err != nil { //nolint:contextcheck // a shared fetch runs under the cache's life
			v.log.Warn("reading a volume object failed", "bucket", ver.bucket, "key", ver.name, "frame", frame, "error", err)
			if started {
				panic(http.ErrAbortHandler)
			}
			v.wrote(ver.bucket, ver.name)
			s3Error(w, http.StatusServiceUnavailable, "SlowDown")
			return true
		}
		if !started {
			started = true
			for name, values := range o.header {
				w.Header()[name] = values
			}
			w.Header().Set("Accept-Ranges", "bytes")
			w.Header().Set("Content-Length", strconv.FormatInt(last-first+1, 10))
			code := http.StatusOK
			if r.Header.Get("Range") != "" {
				w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", first, last, ver.size))
				code = http.StatusPartialContent
			}
			w.WriteHeader(code)
		}
		if _, err := w.Write(p); err != nil {
			return true
		}
		pos += int64(len(p))
	}
	return true
}

// byteRange reads a Range header of the form bytes=first-last within
// size, or none for the whole object.
func byteRange(header string, size int64) (first, last int64, ok bool) {
	if header == "" {
		return 0, size - 1, true
	}
	spec, found := strings.CutPrefix(header, "bytes=")
	a, b, _ := strings.Cut(spec, "-")
	first, errA := strconv.ParseInt(a, 10, 64)
	last, errB := strconv.ParseInt(b, 10, 64)
	if !found || errA != nil || errB != nil || first < 0 || first > last || first >= size {
		return 0, 0, false
	}
	return first, min(last, size-1), true
}

func s3Error(w http.ResponseWriter, code int, s3Code string) {
	w.Header().Set("Content-Type", "application/xml")
	w.WriteHeader(code)
	_, _ = fmt.Fprintf(w, `<?xml version="1.0" encoding="UTF-8"?><Error><Code>%s</Code></Error>`, s3Code)
}

// fill fetches a version whole in the background, unless that runs.
func (v *volumes) fill(ver objectVersion) {
	if v.claimFill(ver) {
		v.cache.background.Go(func() { v.fillClaimed(v.cache.life, ver) })
	}
}

func (v *volumes) claimFill(ver objectVersion) bool {
	v.mu.Lock()
	defer v.mu.Unlock()
	name := ver.key(0).object
	if v.filling[name] {
		return false
	}
	v.filling[name] = true
	return true
}

// fillClaimed fetches the frames of a version the cache lacks, behind
// reads, until ctx ends.
func (v *volumes) fillClaimed(ctx context.Context, ver objectVersion) {
	defer func() {
		v.mu.Lock()
		delete(v.filling, ver.key(0).object)
		v.mu.Unlock()
	}()
	began := time.Now()
	fetched := v.cache.loadEach(ctx, ver.frames())
	v.log.Info("volume object filled", "bucket", ver.bucket, "key", ver.name, "bytes", ver.size, "fetched_frames", fetched,
		"seconds", time.Since(began).Seconds(), "error", ctx.Err())
}

// warm fills the objects of 1 MiB or more under prefixes of the
// workspace's bucket, each within a quarter of the cache and all within
// half, one after another, until ctx ends.
func (v *volumes) warm(ctx context.Context, g volumeGrant, prefixes []string) {
	began := time.Now()
	budget, objects := v.cache.limit/2, 0
	for _, prefix := range prefixes {
		for o, err := range g.objects.List(ctx, prefix) {
			if err != nil {
				v.log.Warn("listing a volume to warm failed", "bucket", g.name, "prefix", prefix, "error", err)
				break
			}
			if o.Size < minCachedObject || o.Size > v.cache.limit/4 {
				continue
			}
			if budget -= o.Size; budget < 0 {
				break
			}
			ver := objectVersion{v: v, workspace: g.workspace, bucket: g.name, name: o.Key, etag: o.ETag, size: o.Size}
			if v.claimFill(ver) {
				v.fillClaimed(ctx, ver)
				objects++
			}
		}
	}
	v.log.Info("volume warm ended", "bucket", g.name, "objects", objects, "seconds", time.Since(began).Seconds(), "error", ctx.Err())
}

// objectVersion is one version of a volume object, read by frames of
// volumeFrame through its bucket's grant.
type objectVersion struct {
	v                             *volumes
	workspace, bucket, name, etag string
	size                          int64
}

func (o objectVersion) key(frame int) frameKey {
	return frameKey{object: "volume:" + o.workspace + "/" + o.bucket + "/" + o.name + "@" + o.etag, frame: frame}
}

func (o objectVersion) frameLen(frame int) int {
	return int(min(volumeFrame, o.size-int64(frame)*volumeFrame))
}

func (o objectVersion) frames() iter.Seq2[frameSource, int] {
	return func(yield func(frameSource, int) bool) {
		for i := range int((o.size + volumeFrame - 1) / volumeFrame) {
			if !yield(o, i) {
				return
			}
		}
	}
}

// fetch reads frame while the object is this version: a 412 fails it.
func (o objectVersion) fetch(ctx context.Context, frame int) ([]byte, error) {
	p := make([]byte, o.frameLen(frame))
	err := retry(ctx, func() error {
		o.v.mu.Lock()
		b := o.v.buckets[o.bucket]
		o.v.mu.Unlock()
		if b == nil || !time.Now().Before(b.grant.creds.Expires) {
			return fmt.Errorf("bucket %s: %w", o.bucket, errNoGrant)
		}
		return b.grant.objects.GetRange(ctx, o.name, o.etag, int64(frame)*volumeFrame, p) //nolint:wrapcheck // wrapped below
	})
	if err != nil {
		return nil, fmt.Errorf("read frame %d of s3://%s/%s: %w", frame, o.bucket, o.name, err)
	}
	return p, nil
}

// volumeSources serves VolumeSources on the snapshotter's socket.
type volumeSources struct {
	imagefsproto.UnimplementedVolumeSourcesServer
	volumes *volumes
}

func (s volumeSources) GrantVolumes(_ context.Context, req *imagefsproto.GrantVolumesRequest) (*imagefsproto.GrantVolumesResponse, error) {
	if req.GetWorkspaceId() != "" {
		if err := s.volumes.grant(req.GetWorkspaceId(), req.GetKey(), req.GetGrant()); err != nil {
			return nil, err
		}
	}
	return &imagefsproto.GrantVolumesResponse{Endpoint: s.volumes.addr}, nil
}

func (s volumeSources) WarmVolumes(_ context.Context, req *imagefsproto.WarmVolumesRequest) (*imagefsproto.WarmVolumesResponse, error) {
	v := s.volumes
	var g volumeGrant
	v.mu.Lock()
	for _, b := range v.buckets {
		if b.grant.workspace == req.GetWorkspaceId() && time.Now().Before(b.grant.creds.Expires) {
			g = b.grant
		}
	}
	v.mu.Unlock()
	if g.name == "" {
		return nil, status.Errorf(codes.FailedPrecondition, "workspace %s has no volume grant", req.GetWorkspaceId())
	}
	for _, p := range req.GetPrefixes() {
		if !strings.HasPrefix(p, volumesPrefix) {
			return nil, status.Errorf(codes.InvalidArgument, "prefix %q is not a volume's", p)
		}
	}
	prefixes := req.GetPrefixes()
	//nolint:contextcheck // a warm lives with the cache, not the call
	v.cache.background.Go(func() {
		ctx, cancel := context.WithTimeout(v.cache.life, prefetchLife)
		defer cancel()
		v.warm(ctx, g, prefixes)
	})
	return &imagefsproto.WarmVolumesResponse{}, nil
}
