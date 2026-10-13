package snapshotter

import (
	"context"
	"fmt"
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
	// volumeFrame is the part of an object one fetch reads and the cache
	// keeps as one frame.
	volumeFrame = 8 << 20
	// minCachedObject is the smallest object reads take from the cache;
	// smaller ones are forwarded.
	minCachedObject = 1 << 20
	// observationLife is how long a store's answer to HEAD names an
	// object's version, as GeeseFS's --stat-cache-ttl.
	observationLife = time.Second
	// maxObservations bounds the versions kept per bucket before expired
	// ones are dropped.
	maxObservations = 4096
	// unsignedPayload leaves forwarded bodies unhashed, so they stream.
	unsignedPayload = "UNSIGNED-PAYLOAD"
)

// replayed are the headers of a HEAD that a GET served from the cache
// answers with, beside the x-amz-meta- ones.
var replayed = []string{"Etag", "Last-Modified", "Content-Type"} //nolint:gochecknoglobals // constant list

// volumes serves the S3 endpoint of the host's platform volumes: it
// forwards requests to each bucket's store, signed with the bucket's grant,
// and answers plain GETs of large objects from the frame cache. The store
// is the cache's durable source.
type volumes struct {
	cache  *frameCache
	http   *http.Client
	log    *slog.Logger
	proxy  *httputil.ReverseProxy
	signer *v4.Signer
	// grants holds each bucket's grant by bucket name.
	grants grants[string, volumeGrant]
	// heads makes concurrent reads of one object share one HEAD.
	heads singleflight.Group

	mu sync.Mutex
	// buckets names each granted workspace's bucket.
	buckets map[string]string
	// seen holds the versions HEADs reported, by bucket and key, and epoch
	// counts each bucket's writes: a HEAD sent before a write began or
	// ended names no version after it.
	seen  map[string]map[string]observation
	epoch map[string]uint64
	// filling names the versions being fetched whole.
	filling map[string]bool
}

// volumeGrant is one workspace bucket's grant: where its store is and the
// credential requests to it are signed with.
type volumeGrant struct {
	workspace, bucket string
	endpoint          *url.URL
	region            string
	pathStyle         bool
	creds             aws.Credentials
	objects           imagefs.Bucket
}

// observation is an object's version as a HEAD reported it at a time, and
// the headers a GET of it answers with.
type observation struct {
	version objectVersion
	header  http.Header
	at      time.Time
}

func newVolumes(cache *frameCache, client *http.Client, log *slog.Logger) *volumes {
	v := &volumes{
		cache: cache, http: client, log: log,
		signer:  v4.NewSigner(func(o *v4.SignerOptions) { o.DisableURIPathEscaping = true }),
		buckets: map[string]string{}, seen: map[string]map[string]observation{}, epoch: map[string]uint64{}, filling: map[string]bool{},
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

// grant records a workspace bucket's grant unless the current one expires
// later.
func (v *volumes) grant(workspace string, g *imagefsproto.BucketGrant) error {
	if _, err := uuid.Parse(workspace); err != nil {
		return status.Error(codes.InvalidArgument, "workspace_id is not a workspace id")
	}
	creds, err := grantCredentials(g)
	if err != nil {
		return err
	}
	endpoint, err := url.Parse(g.GetEndpoint())
	if err != nil || endpoint.Host == "" || (endpoint.Scheme != "https" && endpoint.Scheme != "http") {
		return status.Error(codes.InvalidArgument, "a volume grant needs an absolute HTTP endpoint")
	}
	v.grants.put(g.GetBucket(), creds.Expires, func() volumeGrant {
		return volumeGrant{
			workspace: workspace, bucket: g.GetBucket(), endpoint: endpoint, region: g.GetRegion(), pathStyle: g.GetForcePathStyle(), creds: creds,
			objects: grantBucket(v.http, g, creds),
		}
	})
	v.mu.Lock()
	v.buckets[workspace] = g.GetBucket()
	v.mu.Unlock()
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

// forwarded is what a forwarded request carries to rewrite and observe.
type forwarded struct {
	grant volumeGrant
	// key is the object's, empty for a bucket request; path is its escaped
	// form.
	key, path string
	epoch     uint64
}

type forwardedKey struct{}

// ServeHTTP answers a path-style S3 request for a granted bucket.
func (v *volumes) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	bucket, path, _ := strings.Cut(strings.TrimPrefix(r.URL.EscapedPath(), "/"), "/")
	g, ok := v.grants.lookup(bucket)
	if !ok {
		s3Error(w, http.StatusForbidden, "AccessDenied")
		return
	}
	key, err := url.PathUnescape(path)
	if err != nil {
		s3Error(w, http.StatusBadRequest, "InvalidURI")
		return
	}
	f := forwarded{grant: g, key: key, path: "/" + path}
	switch r.Method {
	case http.MethodPut, http.MethodPost, http.MethodDelete:
		v.forget(bucket)
		defer v.forget(bucket)
	case http.MethodGet:
		if key != "" && r.URL.RawQuery == "" && v.serveCached(w, r, f) {
			return
		}
	}
	f.epoch = v.epochOf(bucket)
	v.proxy.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), forwardedKey{}, f)))
}

// rewrite points a forwarded request at its bucket's store and signs it.
func (v *volumes) rewrite(pr *httputil.ProxyRequest) {
	f := pr.In.Context().Value(forwardedKey{}).(forwarded) //nolint:forcetypeassert // ServeHTTP sets it
	pr.Out.URL = f.grant.url(f.path, pr.In.URL.RawQuery)
	pr.Out.Host = ""
	if err := v.sign(pr.Out, f.grant); err != nil {
		v.log.Warn("signing a volume request failed", "bucket", f.grant.bucket, "error", err)
	}
}

// url is where path, an escaped object path with its leading slash, is
// in the grant's store.
func (g volumeGrant) url(path, query string) *url.URL {
	u := *g.endpoint
	raw := strings.TrimSuffix(u.EscapedPath(), "/")
	if g.pathStyle {
		raw += "/" + g.bucket
	} else {
		u.Host = g.bucket + "." + u.Host
	}
	raw += path
	u.Path, _ = url.PathUnescape(raw)
	u.RawPath, u.RawQuery = raw, query
	return &u
}

// sign replaces the client's signature with the grant's.
func (v *volumes) sign(r *http.Request, g volumeGrant) error {
	for _, h := range []string{"Authorization", "X-Amz-Date", "X-Amz-Security-Token", "X-Amz-Content-Sha256"} {
		r.Header.Del(h)
	}
	r.Header.Set("X-Amz-Content-Sha256", unsignedPayload)
	if g.creds.SessionToken != "" {
		r.Header.Set("X-Amz-Security-Token", g.creds.SessionToken)
	}
	return v.signer.SignHTTP(r.Context(), g.creds, r, unsignedPayload, "s3", g.region, time.Now()) //nolint:wrapcheck // rewrite logs it
}

// observe records the version a forwarded HEAD or GET of an object reports.
func (v *volumes) observe(resp *http.Response) error {
	f, _ := resp.Request.Context().Value(forwardedKey{}).(forwarded)
	if f.key == "" || (resp.Request.Method != http.MethodHead && resp.Request.Method != http.MethodGet) {
		return nil
	}
	size := resp.ContentLength
	switch resp.StatusCode {
	case http.StatusOK:
	case http.StatusPartialContent:
		_, total, _ := strings.Cut(resp.Header.Get("Content-Range"), "/")
		var err error
		if size, err = strconv.ParseInt(total, 10, 64); err != nil {
			return nil
		}
	default:
		return nil
	}
	etag := resp.Header.Get("Etag")
	if etag == "" || size < 0 {
		return nil
	}
	header := http.Header{}
	for name, values := range resp.Header {
		if strings.HasPrefix(strings.ToLower(name), "x-amz-meta-") {
			header[name] = values
		}
	}
	for _, name := range replayed {
		if value := resp.Header.Get(name); value != "" {
			header.Set(name, value)
		}
	}
	ver := objectVersion{v: v, workspace: f.grant.workspace, bucket: f.grant.bucket, name: f.key, etag: etag, size: size}
	v.mu.Lock()
	defer v.mu.Unlock()
	if v.epoch[f.grant.bucket] != f.epoch {
		return nil
	}
	seen := v.seen[f.grant.bucket]
	if seen == nil {
		seen = map[string]observation{}
		v.seen[f.grant.bucket] = seen
	}
	now := time.Now()
	if len(seen) >= maxObservations {
		for k, o := range seen {
			if now.Sub(o.at) > observationLife {
				delete(seen, k)
			}
		}
	}
	seen[f.key] = observation{version: ver, header: header, at: now}
	return nil
}

func (v *volumes) epochOf(bucket string) uint64 {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.epoch[bucket]
}

// forget drops the bucket's observed versions, so reads after a write ask
// the store again.
func (v *volumes) forget(bucket string) {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.epoch[bucket]++
	delete(v.seen, bucket)
}

// version returns the object's version, observed within observationLife
// or asked of the store with a HEAD. False means the store named none, or
// could not be asked, and the GET is forwarded.
func (v *volumes) version(ctx context.Context, f forwarded) (observation, bool) {
	if o, ok := v.observed(f.grant.bucket, f.key); ok {
		return o, true
	}
	_, err, _ := v.heads.Do(f.grant.bucket+"/"+f.key, func() (any, error) {
		f.epoch = v.epochOf(f.grant.bucket)
		req, err := http.NewRequestWithContext(context.WithValue(context.WithoutCancel(ctx), forwardedKey{}, f), http.MethodHead, f.grant.url(f.path, "").String(), nil)
		if err != nil {
			return nil, fmt.Errorf("ask an object's version: %w", err)
		}
		if err := v.sign(req, f.grant); err != nil {
			return nil, err
		}
		resp, err := v.http.Do(req)
		if err != nil {
			return nil, fmt.Errorf("ask an object's version: %w", err)
		}
		_ = resp.Body.Close()
		return nil, v.observe(resp)
	})
	if err != nil {
		v.log.Warn("asking a volume object's version failed", "bucket", f.grant.bucket, "key", f.key, "error", err)
		return observation{}, false
	}
	return v.observed(f.grant.bucket, f.key)
}

func (v *volumes) observed(bucket, key string) (observation, bool) {
	v.mu.Lock()
	defer v.mu.Unlock()
	o, ok := v.seen[bucket][key]
	return o, ok && time.Since(o.at) <= observationLife
}

// serveCached answers a GET of a large object from the cache and reports
// whether it did; a GET it does not answer is forwarded. A whole object
// within a quarter of the cache is fetched in the background. A frame the
// store refuses fails the GET as 503, which GeeseFS retries, or once the
// answer began, cuts it short.
func (v *volumes) serveCached(w http.ResponseWriter, r *http.Request, f forwarded) bool {
	o, ok := v.version(r.Context(), f)
	if !ok || o.version.size < minCachedObject {
		return false
	}
	ver := o.version
	first, last, ranged, ok := byteRange(r.Header.Get("Range"), ver.size)
	if !ok {
		return false
	}
	if ver.size <= v.cache.limit/4 {
		v.fill(ver)
	}
	buf := make([]byte, min(maxRead, last-first+1))
	for pos, started := first, false; pos <= last; {
		frame, within := int(pos/volumeFrame), pos%volumeFrame
		p := buf[:min(int64(len(buf)), volumeFrame-within, last-pos+1)]
		if _, _, err := v.cache.read(ver, frame, p, within); err != nil { //nolint:contextcheck // a shared fetch runs under the cache's life
			v.log.Warn("reading a volume object failed", "bucket", ver.bucket, "key", ver.name, "frame", frame, "error", err)
			if !started {
				v.forget(ver.bucket)
				s3Error(w, http.StatusServiceUnavailable, "SlowDown")
				return true
			}
			panic(http.ErrAbortHandler)
		}
		if !started {
			started = true
			for name, values := range o.header {
				w.Header()[name] = values
			}
			w.Header().Set("Accept-Ranges", "bytes")
			w.Header().Set("Content-Length", strconv.FormatInt(last-first+1, 10))
			code := http.StatusOK
			if ranged {
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

// byteRange reads a Range header of one range within size; false for one
// the store should answer.
func byteRange(header string, size int64) (first, last int64, ranged, ok bool) {
	if header == "" {
		return 0, size - 1, false, true
	}
	spec, found := strings.CutPrefix(header, "bytes=")
	a, b, dash := strings.Cut(spec, "-")
	if !found || !dash || strings.Contains(spec, ",") {
		return 0, 0, false, false
	}
	var err error
	switch {
	case a == "":
		n, err := strconv.ParseInt(b, 10, 64)
		if err != nil || n <= 0 {
			return 0, 0, false, false
		}
		return max(0, size-n), size - 1, true, true
	case b == "":
		last = size - 1
	default:
		if last, err = strconv.ParseInt(b, 10, 64); err != nil {
			return 0, 0, false, false
		}
	}
	if first, err = strconv.ParseInt(a, 10, 64); err != nil || first > last || first >= size {
		return 0, 0, false, false
	}
	return first, min(last, size-1), true, true
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
				v.log.Warn("listing a volume to warm failed", "bucket", g.bucket, "prefix", prefix, "error", err)
				break
			}
			if o.Size < minCachedObject || o.Size > v.cache.limit/4 {
				continue
			}
			if budget -= o.Size; budget < 0 {
				break
			}
			ver := objectVersion{v: v, workspace: g.workspace, bucket: g.bucket, name: o.Key, etag: o.ETag, size: o.Size}
			if v.claimFill(ver) {
				v.fillClaimed(ctx, ver)
				objects++
			}
		}
	}
	v.log.Info("volume warm ended", "bucket", g.bucket, "objects", objects, "seconds", time.Since(began).Seconds(), "error", ctx.Err())
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
		g, ok := o.v.grants.lookup(o.bucket)
		if !ok {
			return fmt.Errorf("bucket %s: %w", o.bucket, errNoGrant)
		}
		return g.objects.GetRange(ctx, o.name, o.etag, int64(frame)*volumeFrame, p) //nolint:wrapcheck // wrapped below
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
	return &imagefsproto.GrantVolumesResponse{}, s.volumes.grant(req.GetWorkspaceId(), req.GetGrant())
}

func (s volumeSources) WarmVolumes(_ context.Context, req *imagefsproto.WarmVolumesRequest) (*imagefsproto.WarmVolumesResponse, error) {
	if len(req.GetPrefixes()) == 0 {
		return &imagefsproto.WarmVolumesResponse{}, nil
	}
	v := s.volumes
	v.mu.Lock()
	bucket := v.buckets[req.GetWorkspaceId()]
	v.mu.Unlock()
	g, ok := v.grants.lookup(bucket)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "workspace %s has no volume grant", req.GetWorkspaceId())
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
