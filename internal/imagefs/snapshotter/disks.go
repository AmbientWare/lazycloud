package snapshotter

import (
	"cmp"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"iter"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
	gofs "github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

// diskTraceWindow is how long after an attach a disk's reads make its start
// trace.
const diskTraceWindow = time.Minute

// disks serves disk generations as read-only files in one FUSE directory,
// named <disk id>-<generation>. Each generation holds its frames in the
// cache while it is served.
type disks struct {
	cache  *frameCache
	dir    string
	http   *http.Client
	log    *slog.Logger
	inodes atomic.Uint64
	// root and server are the mounted directory, set by mount.
	mountMu sync.Mutex
	root    *gofs.Inode
	server  *fuse.Server

	mu   sync.Mutex
	byID map[string]*disk
}

// disk is one disk the agent granted: its credential, the generations
// served, and its reads.
type disk struct {
	id    string
	grant atomic.Pointer[diskGrant]

	mu          sync.Mutex
	generations map[int64]*diskGeneration
	// traceStart is when the latest prefetching serve began the start
	// trace, and trace the frames first read since, within the window.
	traceStart time.Time
	trace      []uint32
	traced     map[uint32]bool
	// lastRead is when each frame was last read.
	lastRead map[uint32]time.Time
}

// diskGrant reads one disk's objects until expires.
type diskGrant struct {
	client  *s3.Client
	bucket  string
	prefix  string
	expires time.Time
}

// diskGeneration is one served generation of a disk.
type diskGeneration struct {
	disk       *disk
	generation int64
	index      imagefs.DiskIndex
	name       string
	cache      *frameCache
	// stopPrefetch ends the generation's prefetch; nil until one starts.
	// Guarded by disk.mu.
	stopPrefetch context.CancelFunc
}

func newDisks(dir string, cache *frameCache, client *http.Client, log *slog.Logger) *disks {
	return &disks{cache: cache, dir: dir, http: client, log: log, byID: map[string]*disk{}}
}

// mount mounts the directory disk generations are served in, once, when
// the first generation is served. Only its owner reads it: the snapshotter,
// the disk engine's daemons and the agent all run as root.
func (d *disks) mount() error {
	d.mountMu.Lock()
	defer d.mountMu.Unlock()
	if d.server != nil {
		return nil
	}
	if err := os.MkdirAll(d.dir, 0o700); err != nil {
		return fmt.Errorf("create the disk directory: %w", err)
	}
	root := &gofs.Inode{}
	zero := time.Duration(0)
	server, err := gofs.Mount(d.dir, root, &gofs.Options{
		MountOptions: fuse.MountOptions{
			FsName: "disks",
			// Reads return bytes in memory; there is no file to splice.
			DisableSplice:    true,
			Name:             layersource.Snapshotter,
			Options:          []string{"ro", "default_permissions"},
			DirectMount:      true,
			DirectMountFlags: syscall.MS_RDONLY,
			MaxWrite:         maxRead,
			MaxReadAhead:     maxRead,
		},
		EntryTimeout:    &zero,
		AttrTimeout:     &zero,
		NegativeTimeout: &zero,
		NullPermissions: true,
	})
	if err != nil {
		return fmt.Errorf("mount the disk directory at %s: %w", d.dir, err)
	}
	d.root, d.server = root, server
	return nil
}

// close unmounts the directory, if mounted. Files still open fail their
// reads.
func (d *disks) close() error {
	d.mountMu.Lock()
	defer d.mountMu.Unlock()
	if d.server == nil {
		return nil
	}
	if err := d.server.Unmount(); err != nil {
		return fmt.Errorf("unmount the disk directory: %w", err)
	}
	return nil
}

func (d *disks) lookup(id string) *disk {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.byID[id]
}

// grant records a disk's credential unless the current one expires later.
func (d *disks) grant(id string, g *imagefsproto.DiskGrant) error {
	if g.GetEndpoint() == "" || g.GetRegion() == "" || g.GetBucket() == "" || g.GetAccessKeyId() == "" || g.GetExpiresAt() == nil {
		return status.Error(codes.InvalidArgument, "a disk grant needs an endpoint, a region, a bucket, a key and an expiry")
	}
	next := &diskGrant{
		bucket: g.GetBucket(), prefix: g.GetPrefix() + "disks/" + id + "/", expires: g.GetExpiresAt().AsTime(),
		client: s3.New(s3.Options{
			Region: g.GetRegion(), BaseEndpoint: aws.String(g.GetEndpoint()), UsePathStyle: g.GetForcePathStyle(),
			Credentials:                aws.NewCredentialsCache(aws.CredentialsProviderFunc(staticCredentials(g))),
			RequestChecksumCalculation: aws.RequestChecksumCalculationWhenRequired,
			ResponseChecksumValidation: aws.ResponseChecksumValidationWhenRequired,
			HTTPClient:                 d.http,
			RetryMaxAttempts:           1,
		}),
	}
	d.mu.Lock()
	k := d.byID[id]
	if k == nil {
		k = &disk{id: id, generations: map[int64]*diskGeneration{}, traced: map[uint32]bool{}, lastRead: map[uint32]time.Time{}}
		d.byID[id] = k
	}
	d.mu.Unlock()
	for {
		current := k.grant.Load()
		if current != nil && current.expires.After(next.expires) || k.grant.CompareAndSwap(current, next) {
			return nil
		}
	}
}

func staticCredentials(g *imagefsproto.DiskGrant) func(context.Context) (aws.Credentials, error) {
	return func(context.Context) (aws.Credentials, error) {
		return aws.Credentials{
			AccessKeyID: g.GetAccessKeyId(), SecretAccessKey: g.GetSecretAccessKey(), SessionToken: g.GetSessionToken(),
			CanExpire: true, Expires: g.GetExpiresAt().AsTime(),
		}, nil
	}
}

// liveGrant returns the disk's grant if it has not expired.
func (k *disk) liveGrant() (*diskGrant, error) {
	g := k.grant.Load()
	if g == nil || !time.Now().Before(g.expires) {
		return nil, fmt.Errorf("disk %s: %w", k.id, errNoGrant)
	}
	return g, nil
}

// get reads the object at key, at most limit bytes.
func (g *diskGrant) get(ctx context.Context, key string, limit int64) ([]byte, error) {
	out, err := g.client.GetObject(ctx, &s3.GetObjectInput{Bucket: &g.bucket, Key: &key})
	if err != nil {
		var missing *types.NoSuchKey
		if errors.As(err, &missing) {
			return nil, fmt.Errorf("%w: s3://%s/%s is missing", imagefs.ErrInvalidIndex, g.bucket, key)
		}
		return nil, fmt.Errorf("get s3://%s/%s: %w", g.bucket, key, err)
	}
	defer func() { _ = out.Body.Close() }()
	data, err := io.ReadAll(io.LimitReader(out.Body, limit+1))
	if err != nil {
		return nil, fmt.Errorf("read s3://%s/%s: %w", g.bucket, key, err)
	}
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("%w: s3://%s/%s is larger than %d bytes", imagefs.ErrInvalidIndex, g.bucket, key, limit)
	}
	return data, nil
}

// serve serves a generation and returns its file, fetching its index the
// first time. With prefetch it begins the disk's start trace and fetches
// the index's start and recent frames in the background.
func (d *disks) serve(ctx context.Context, req *imagefsproto.ServeDiskRequest) (*diskGeneration, error) {
	k := d.lookup(req.GetDiskId())
	if k == nil {
		return nil, status.Errorf(codes.FailedPrecondition, "disk %s has no grant", req.GetDiskId())
	}
	k.mu.Lock()
	g := k.generations[req.GetGeneration()]
	k.mu.Unlock()
	if g == nil {
		var err error
		if g, err = d.open(ctx, k, req); err != nil {
			return nil, err
		}
	}
	if req.GetPrefetch() {
		k.mu.Lock()
		k.traceStart, k.trace = time.Now(), nil
		clear(k.traced)
		// A generation served again, as by a repeated attach, keeps its
		// prefetch.
		var life context.Context
		prefetch := g.stopPrefetch == nil
		if prefetch {
			life, g.stopPrefetch = context.WithTimeout(d.cache.life, prefetchLife) //nolint:contextcheck // a prefetch lives with the cache, not the call
		}
		stop := g.stopPrefetch
		k.mu.Unlock()
		if prefetch {
			d.cache.background.Go(func() {
				defer stop()
				d.prefetch(life, g)
			})
		}
	}
	if len(req.GetWarm()) > 0 {
		d.cache.loadEach(ctx, g.frames(req.GetWarm()))
	}
	return g, nil
}

// open fetches a generation's index and adds its file.
func (d *disks) open(ctx context.Context, k *disk, req *imagefsproto.ServeDiskRequest) (*diskGeneration, error) {
	if err := d.mount(); err != nil {
		return nil, err
	}
	var raw []byte
	err := retry(ctx, func() error {
		grant, err := k.liveGrant()
		if err != nil {
			return err
		}
		if !strings.HasPrefix(req.GetIndexKey(), grant.prefix) {
			return status.Errorf(codes.InvalidArgument, "index %s is not disk %s's", req.GetIndexKey(), k.id)
		}
		raw, err = grant.get(ctx, req.GetIndexKey(), imagefs.MaxIndexSize)
		return err
	})
	if errors.Is(err, errNoGrant) {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	if err != nil {
		return nil, fmt.Errorf("index of disk %s generation %d: %w", k.id, req.GetGeneration(), err)
	}
	if sum := sha256.Sum256(raw); hex.EncodeToString(sum[:]) != req.GetIndexSha256() {
		return nil, status.Errorf(codes.InvalidArgument, "the index of disk %s generation %d does not match its sha256", k.id, req.GetGeneration())
	}
	ix, err := imagefs.UnmarshalDisk(raw)
	if err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "index of disk %s generation %d: %v", k.id, req.GetGeneration(), err)
	}
	g := &diskGeneration{disk: k, generation: req.GetGeneration(), index: ix, cache: d.cache,
		name: k.id + "-" + strconv.FormatInt(req.GetGeneration(), 10)}
	k.mu.Lock()
	defer k.mu.Unlock()
	if served := k.generations[g.generation]; served != nil {
		return served, nil
	}
	k.generations[g.generation] = g
	d.cache.hold(g.keys(), 1)
	file := d.root.NewPersistentInode(ctx, &diskFile{gen: g}, gofs.StableAttr{Mode: syscall.S_IFREG, Ino: d.inodes.Add(1) + 1})
	d.root.AddChild(g.name, file, true)
	return g, nil
}

// release stops serving every generation of the disk but keep, and forgets
// a disk left with none.
func (d *disks) release(id string, keep int64) {
	k := d.lookup(id)
	if k == nil {
		return
	}
	k.mu.Lock()
	var gone []*diskGeneration
	for n, g := range k.generations {
		if n != keep {
			if g.stopPrefetch != nil {
				g.stopPrefetch()
			}
			gone = append(gone, g)
			delete(k.generations, n)
		}
	}
	empty := len(k.generations) == 0
	k.mu.Unlock()
	for _, g := range gone {
		d.root.RmChild(g.name)
		if errno := d.root.NotifyEntry(g.name); errno != 0 && errno != syscall.ENOENT {
			d.log.Warn("dropping a disk file from the kernel's cache failed", "file", g.name, "error", errno)
		}
		d.cache.hold(g.keys(), -1)
	}
	if empty {
		d.mu.Lock()
		if d.byID[id] == k {
			delete(d.byID, id)
		}
		d.mu.Unlock()
	}
}

// prefetch fetches g's start frames, then its recent ones, at most a
// quarter of the cache, until ctx ends.
func (d *disks) prefetch(ctx context.Context, g *diskGeneration) {
	began := time.Now()
	order := slices.Concat(g.index.Start, g.index.Recent)
	seen := make(map[uint32]bool, len(order))
	order = slices.DeleteFunc(order, func(i uint32) bool {
		skip := seen[i] || g.index.Frames[i].Zero()
		seen[i] = true
		return skip
	})
	order = order[:min(len(order), d.cache.prefetchFrames())]
	fetched := d.cache.loadEach(ctx, g.frames(order))
	d.log.Info("disk prefetch ended", "disk_id", g.disk.id, "generation", g.generation, "frames", len(order),
		"fetched", fetched, "seconds", time.Since(began).Seconds(), "error", ctx.Err())
}

// prefetchFrames bounds the frames one prefetch fetches: a quarter of the
// cache, so it never evicts most of what containers use.
func (c *frameCache) prefetchFrames() int { return int(c.limit / imagefs.FrameSize / 4) }

// reads returns the disk's start trace, once its window has passed, and
// the frames of its newest generation the cache holds, most recently read
// first.
func (d *disks) reads(id string) (*imagefsproto.DiskReadsResponse, error) {
	k := d.lookup(id)
	if k == nil {
		return nil, status.Errorf(codes.NotFound, "disk %s is not served", id)
	}
	k.mu.Lock()
	out := &imagefsproto.DiskReadsResponse{}
	if !k.traceStart.IsZero() && time.Since(k.traceStart) >= diskTraceWindow {
		out.StartFrames = slices.Clone(k.trace)
	}
	var newest *diskGeneration
	for _, g := range k.generations {
		if newest == nil || g.generation > newest.generation {
			newest = g
		}
	}
	type read struct {
		frame uint32
		at    time.Time
	}
	recent := make([]read, 0, len(k.lastRead))
	for frame, at := range k.lastRead {
		recent = append(recent, read{frame, at})
	}
	k.mu.Unlock()
	if newest == nil {
		return out, nil
	}
	slices.SortFunc(recent, func(a, b read) int { return cmp.Or(b.at.Compare(a.at), cmp.Compare(a.frame, b.frame)) })
	for _, r := range recent {
		if len(out.RecentFrames) >= d.cache.prefetchFrames() {
			break
		}
		if i := int(r.frame); i < len(newest.index.Frames) && !newest.index.Frames[i].Zero() && d.cache.cached(newest.key(i)) {
			out.RecentFrames = append(out.RecentFrames, r.frame)
		}
	}
	return out, nil
}

// record counts a read of frame in the disk's start trace and recency.
func (k *disk) record(frame uint32) {
	now := time.Now()
	k.mu.Lock()
	defer k.mu.Unlock()
	k.lastRead[frame] = now
	if !k.traceStart.IsZero() && now.Sub(k.traceStart) < diskTraceWindow && !k.traced[frame] && len(k.trace) < imagefs.MaxTraceReads {
		k.traced[frame] = true
		k.trace = append(k.trace, frame)
	}
}

func (g *diskGeneration) key(frame int) frameKey {
	return frameKey{object: "disk:" + hex.EncodeToString(g.index.Frames[frame].Digest[:])}
}

func (g *diskGeneration) frameLen(frame int) int { return g.index.FrameLen(frame) }

// keys yields the keys of the generation's stored frames.
func (g *diskGeneration) keys() iter.Seq[frameKey] {
	return func(yield func(frameKey) bool) {
		for i, f := range g.index.Frames {
			if !f.Zero() && !yield(g.key(i)) {
				return
			}
		}
	}
}

// frames yields the stored frames among list.
func (g *diskGeneration) frames(list []uint32) iter.Seq2[frameSource, int] {
	return func(yield func(frameSource, int) bool) {
		for _, i := range list {
			if int(i) < len(g.index.Frames) && !g.index.Frames[i].Zero() && !yield(g, int(i)) {
				return
			}
		}
	}
}

// fetch reads frame's stored copy through the disk's grant and checks it
// against its digest.
func (g *diskGeneration) fetch(ctx context.Context, frame int) ([]byte, error) {
	f := g.index.Frames[frame]
	var data []byte
	err := retry(ctx, func() error {
		grant, err := g.disk.liveGrant()
		if err != nil {
			return err
		}
		packed, err := grant.get(ctx, grant.prefix+f.Name(), f.Size)
		if err != nil {
			return err
		}
		data, err = g.cache.reader.DecodeDiskFrame(packed, f, g.index.FrameLen(frame))
		return err //nolint:wrapcheck // wrapped below
	})
	if err != nil {
		return nil, fmt.Errorf("read frame %d of disk %s generation %d: %w", frame, g.disk.id, g.generation, err)
	}
	return data, nil
}

// diskFile is a served generation's file.
type diskFile struct {
	gofs.Inode
	gen *diskGeneration
}

var (
	_ gofs.NodeGetattrer = (*diskFile)(nil)
	_ gofs.NodeOpener    = (*diskFile)(nil)
	_ gofs.NodeReader    = (*diskFile)(nil)
	_ gofs.NodeLseeker   = (*diskFile)(nil)
)

func (f *diskFile) Getattr(_ context.Context, _ gofs.FileHandle, out *fuse.AttrOut) syscall.Errno {
	out.Mode = syscall.S_IFREG | 0o400
	out.Nlink = 1
	out.Size = uint64(f.gen.index.Size) //nolint:gosec // sizes are validated positive
	out.Blksize = imagefs.DiskBlockBytes
	return 0
}

// untraced is the handle of a file opened with O_NOATIME, as the disk
// engine's publish opens it: its reads are not the disk's use.
type untraced struct{}

// Open serves reads directly: the disk engine's daemon caches what it
// needs, and the frame cache holds the rest.
func (f *diskFile) Open(_ context.Context, flags uint32) (gofs.FileHandle, uint32, syscall.Errno) {
	if flags&(syscall.O_WRONLY|syscall.O_RDWR|syscall.O_TRUNC|syscall.O_APPEND) != 0 {
		return nil, 0, syscall.EROFS
	}
	if flags&syscall.O_NOATIME != 0 {
		return untraced{}, fuse.FOPEN_DIRECT_IO, 0
	}
	return nil, fuse.FOPEN_DIRECT_IO, 0
}

// Read fills dest from off, frame by frame; a frame of zeros reads as
// zeros. A read the store cannot serve fails whole with EIO, logged.
func (f *diskFile) Read(ctx context.Context, fh gofs.FileHandle, dest []byte, off int64) (fuse.ReadResult, syscall.Errno) {
	g := f.gen
	_, quiet := fh.(untraced)
	dest = dest[:max(0, min(int64(len(dest)), g.index.Size-off))]
	for done := 0; done < len(dest); {
		pos := off + int64(done)
		frame, within := int(pos/imagefs.FrameSize), pos%imagefs.FrameSize
		part := dest[done:min(len(dest), done+int(imagefs.FrameSize-within))]
		if g.index.Frames[frame].Zero() {
			clear(part)
			done += len(part)
			continue
		}
		if !quiet {
			g.disk.record(uint32(frame)) //nolint:gosec // frames are below maxDiskFrames
		}
		if _, _, err := g.cache.read(g, frame, part, within); err != nil { //nolint:contextcheck // a shared fetch runs under the cache's life
			g.cache.log.ErrorContext(ctx, "disk read failed", "disk_id", g.disk.id, "generation", g.generation, "offset", pos, "error", err)
			return nil, syscall.EIO
		}
		done += len(part)
	}
	return fuse.ReadResultData(dest), 0
}

// Lseek reports frames of zeros as holes.
func (f *diskFile) Lseek(_ context.Context, _ gofs.FileHandle, off uint64, whence uint32) (uint64, syscall.Errno) {
	ix := f.gen.index
	if off >= uint64(ix.Size) { //nolint:gosec // sizes are validated positive
		return 0, syscall.ENXIO
	}
	data := whence == unix.SEEK_DATA
	if !data && whence != unix.SEEK_HOLE {
		return 0, syscall.EINVAL
	}
	for i := int(off / imagefs.FrameSize); i < len(ix.Frames); i++ { //nolint:gosec // off is below the size checked above.
		if ix.Frames[i].Zero() != data {
			return max(off, uint64(i)*imagefs.FrameSize), 0 //nolint:gosec // frame offsets are positive
		}
	}
	if data {
		return 0, syscall.ENXIO
	}
	return uint64(ix.Size), 0 //nolint:gosec // sizes are validated positive
}

// path is where generation g's file is.
func (d *disks) path(g *diskGeneration) string { return filepath.Join(d.dir, g.name) }

// diskSources serves DiskSources on the snapshotter's socket.
type diskSources struct {
	imagefsproto.UnimplementedDiskSourcesServer
	disks *disks
}

func (s diskSources) GrantDisk(_ context.Context, req *imagefsproto.GrantDiskRequest) (*imagefsproto.GrantDiskResponse, error) {
	if !diskIDPattern(req.GetDiskId()) {
		return nil, status.Error(codes.InvalidArgument, "disk_id is not a disk id")
	}
	return &imagefsproto.GrantDiskResponse{}, s.disks.grant(req.GetDiskId(), req.GetGrant())
}

func (s diskSources) ServeDisk(ctx context.Context, req *imagefsproto.ServeDiskRequest) (*imagefsproto.ServeDiskResponse, error) {
	if req.GetGeneration() <= 0 || req.GetIndexKey() == "" {
		return nil, status.Error(codes.InvalidArgument, "a served generation needs a number and an index key")
	}
	g, err := s.disks.serve(ctx, req)
	if err != nil {
		return nil, err
	}
	return &imagefsproto.ServeDiskResponse{Path: s.disks.path(g)}, nil
}

func (s diskSources) ReleaseDisk(_ context.Context, req *imagefsproto.ReleaseDiskRequest) (*imagefsproto.ReleaseDiskResponse, error) {
	s.disks.release(req.GetDiskId(), req.GetKeep())
	return &imagefsproto.ReleaseDiskResponse{}, nil
}

func (s diskSources) DiskReads(_ context.Context, req *imagefsproto.DiskReadsRequest) (*imagefsproto.DiskReadsResponse, error) {
	return s.disks.reads(req.GetDiskId())
}

// diskIDPattern reports whether id may name a disk's prefix and files: 1 to
// 128 letters, digits, underscores or hyphens.
func diskIDPattern(id string) bool {
	return id != "" && len(id) <= 128 && !slices.ContainsFunc([]byte(id), func(b byte) bool {
		return (b < 'a' || b > 'z') && (b < 'A' || b > 'Z') && (b < '0' || b > '9') && b != '_' && b != '-'
	})
}
