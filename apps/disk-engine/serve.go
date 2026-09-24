package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sync/errgroup"
)

const (
	serveFlushInterval = 2 * time.Second
	// heatWindow matches the worker's publish interval, so one window is one
	// publish's worth of use.
	heatWindow        = 2 * time.Minute
	hydrateHotFetches = 4
	hydrateFetches    = 2
	// hydrateRate leaves most of the machine's bandwidth to the workload's own
	// reads while the chunks nobody asked for yet arrive.
	hydrateRate     = 64 << 20
	serverStartWait = 10 * time.Second
	serverStopWait  = 30 * time.Second
	maxHeatMapBytes = 2 << 20
	heatUploadWait  = 10 * time.Second
)

type serveStatus struct {
	StartedAt     time.Time  `json:"started_at"`
	Layers        int        `json:"layers"`
	Chunks        int        `json:"chunks"`
	PresentChunks int        `json:"present_chunks"`
	Bytes         int64      `json:"bytes"`
	PresentBytes  int64      `json:"present_bytes"`
	HotChunks     int        `json:"hot_chunks"`
	HotDoneAt     *time.Time `json:"hot_done_at,omitempty"`
	DoneAt        *time.Time `json:"done_at,omitempty"`
	// FailedReads counts reads that returned an I/O error to the daemon.
	FailedReads  int        `json:"failed_reads"`
	LastError    string     `json:"last_error,omitempty"`
	LastErrorAt  *time.Time `json:"last_error_at,omitempty"`
	HydrateError string     `json:"hydrate_error,omitempty"`
	HeatError    string     `json:"heat_error,omitempty"`
}

type chunkRef struct{ layer, index int }

// runServe serves the disk's lazy layers to qemu-storage-daemon until it is
// told to stop, and fills them in the background meanwhile.
func runServe(ctx context.Context, args []string) (any, error) {
	f := newFlags("serve", true)
	storePath := f.set.String("store", "", "STORE.json the worker keeps renewed while the disk is attached")
	f.require("store")
	if err := f.parse(args); err != nil {
		return nil, err
	}
	p := f.paths()
	state, err := requireState(p)
	if err != nil {
		return nil, err
	}
	store, err := openStore(*storePath)
	if err != nil {
		return nil, err
	}
	heat, err := loadHeat(p)
	if err != nil {
		return nil, err
	}
	var layers []*lazyLayer
	exports := map[string]nbdExport{}
	defer func() {
		for _, layer := range layers {
			layer.close()
		}
	}()
	for _, l := range state.Layers {
		if !l.Lazy {
			continue
		}
		layer, err := openLazyLayer(p, l, store)
		if err != nil {
			return nil, err
		}
		layer.touched = heat.touch
		layers = append(layers, layer)
		exports[l.file()] = layer
	}
	status := &serveProgress{status: serveStatus{StartedAt: time.Now().UTC(), Layers: len(layers)}}
	if err := os.Remove(p.layersSocket()); err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	listener, err := net.Listen("unix", p.layersSocket())
	if err != nil {
		return nil, err
	}
	server := &nbdServer{exports: exports, failed: status.failedRead}

	group, groupCtx := errgroup.WithContext(ctx)
	group.Go(func() error { return server.serve(groupCtx, listener) })
	group.Go(func() error {
		hydrate(groupCtx, layers, heat.ordered(), status)
		return nil
	})
	group.Go(func() error {
		flushes := time.NewTicker(serveFlushInterval)
		defer flushes.Stop()
		windows := time.NewTicker(heatWindow)
		defer windows.Stop()
		for {
			select {
			case <-groupCtx.Done():
				return nil
			case <-flushes.C:
				status.record(p, layers)
			case <-windows.C:
				heat.age()
				status.heatError(publishHeat(groupCtx, p, store, heat))
			}
		}
	})
	err = group.Wait()
	final, cancel := context.WithTimeout(context.WithoutCancel(ctx), heatUploadWait)
	defer cancel()
	status.heatError(publishHeat(final, p, store, heat))
	status.record(p, layers)
	return status.snapshot(), err
}

// publishHeat saves the map beside the disk and in its bucket prefix, where
// the next restore on any machine finds it.
func publishHeat(ctx context.Context, p diskPaths, store *objectStore, heat *heatMap) error {
	data, err := heat.encode()
	if err != nil {
		return err
	}
	if err := writeFileAtomic(p.heatPath(), data); err != nil {
		return err
	}
	return store.put(ctx, diskHeatKey(p.id), data, "application/octet-stream")
}

// hydrationOrder lists the chunks still missing: those the heat map names,
// most recently used first, then the rest, newest layer first and in file
// order within a layer.
func hydrationOrder(layers []*lazyLayer, hot []heatKey) (first, rest []chunkRef) {
	queued := map[chunkRef]bool{}
	byKey := map[heatKey][]chunkRef{}
	for li := len(layers) - 1; li >= 0; li-- {
		layer := layers[li]
		layer.mu.Lock()
		for i, chunk := range layer.manifest.Chunks {
			if layer.has(i) {
				queued[chunkRef{li, i}] = true
				continue
			}
			if key, ok := heatKeyOf(chunk.SHA256); ok {
				byKey[key] = append(byKey[key], chunkRef{li, i})
			}
		}
		layer.mu.Unlock()
	}
	for _, key := range hot {
		for _, ref := range byKey[key] {
			if !queued[ref] {
				queued[ref] = true
				first = append(first, ref)
			}
		}
	}
	for li := len(layers) - 1; li >= 0; li-- {
		for i := range layers[li].manifest.Chunks {
			if ref := (chunkRef{li, i}); !queued[ref] {
				queued[ref] = true
				rest = append(rest, ref)
			}
		}
	}
	return first, rest
}

func hydrate(ctx context.Context, layers []*lazyLayer, hot []heatKey, status *serveProgress) {
	first, rest := hydrationOrder(layers, hot)
	status.setHot(len(first))
	fetchAll(ctx, layers, first, hydrateHotFetches, nil, status)
	status.markHotDone()
	fetchAll(ctx, layers, rest, hydrateFetches, newRateLimit(hydrateRate), status)
	if ctx.Err() == nil && !slices.ContainsFunc(layers, func(l *lazyLayer) bool { return !l.complete() }) {
		status.markDone()
	}
}

// fetchAll fetches refs in order on a few connections. A chunk a read already
// fetched costs nothing here, and one that cannot be fetched is left for a
// read to try again.
func fetchAll(ctx context.Context, layers []*lazyLayer, refs []chunkRef, fetches int, limit *rateLimit, status *serveProgress) {
	work := make(chan chunkRef)
	var wg sync.WaitGroup
	for range fetches {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for ref := range work {
				layer := layers[ref.layer]
				if err := limit.wait(ctx, layer.manifest.Chunks[ref.index].Length); err != nil {
					return
				}
				if err := layer.fetch(ctx, ref.index); err != nil {
					status.hydrateError(err)
				}
			}
		}()
	}
	for _, ref := range refs {
		select {
		case work <- ref:
		case <-ctx.Done():
		}
		if ctx.Err() != nil {
			break
		}
	}
	close(work)
	wg.Wait()
}

type rateLimit struct {
	mu      sync.Mutex
	perByte time.Duration
	next    time.Time
}

func newRateLimit(bytesPerSecond int64) *rateLimit {
	return &rateLimit{perByte: time.Second / time.Duration(bytesPerSecond)}
}

// wait blocks until length more bytes fit the rate. A nil limit never waits.
func (r *rateLimit) wait(ctx context.Context, length int64) error {
	if r == nil {
		return ctx.Err()
	}
	r.mu.Lock()
	now := time.Now()
	start := r.next
	if start.Before(now) {
		start = now
	}
	r.next = start.Add(time.Duration(length) * r.perByte)
	r.mu.Unlock()
	select {
	case <-time.After(start.Sub(now)):
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

type serveProgress struct {
	mu     sync.Mutex
	status serveStatus
}

func (s *serveProgress) failedRead(export string, err error) {
	now := time.Now().UTC()
	s.mu.Lock()
	defer s.mu.Unlock()
	s.status.FailedReads++
	s.status.LastError = fmt.Sprintf("%s: %v", export, err)
	s.status.LastErrorAt = &now
}

func (s *serveProgress) hydrateError(err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.status.HydrateError = err.Error()
}

func (s *serveProgress) heatError(err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.status.HeatError = ""
	if err != nil {
		s.status.HeatError = err.Error()
	}
}

func (s *serveProgress) setHot(count int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.status.HotChunks = count
}

func (s *serveProgress) markHotDone() {
	now := time.Now().UTC()
	s.mu.Lock()
	defer s.mu.Unlock()
	s.status.HotDoneAt = &now
}

func (s *serveProgress) markDone() {
	now := time.Now().UTC()
	s.mu.Lock()
	defer s.mu.Unlock()
	s.status.DoneAt = &now
}

func (s *serveProgress) snapshot() serveStatus {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.status
}

// record flushes every layer's bitmap and writes the progress file. A flush
// that fails is reported and tried again at the next tick.
func (s *serveProgress) record(p diskPaths, layers []*lazyLayer) {
	var chunks, present int
	var bytes, presentBytes int64
	var flushErr error
	for _, layer := range layers {
		flushErr = errors.Join(flushErr, layer.flush())
		c, pc, b, pb := layer.progress()
		chunks, present, bytes, presentBytes = chunks+c, present+pc, bytes+b, presentBytes+pb
	}
	s.mu.Lock()
	s.status.Chunks, s.status.PresentChunks = chunks, present
	s.status.Bytes, s.status.PresentBytes = bytes, presentBytes
	if flushErr != nil {
		s.status.HydrateError = flushErr.Error()
	}
	status := s.status
	s.mu.Unlock()
	if data, err := json.Marshal(status); err == nil {
		writeFileAtomic(p.serveStatusPath(), data)
	}
}

func readServeStatus(p diskPaths) (serveStatus, error) {
	var status serveStatus
	data, err := os.ReadFile(p.serveStatusPath())
	if errors.Is(err, os.ErrNotExist) {
		return status, nil
	}
	if err != nil {
		return status, err
	}
	return status, json.Unmarshal(data, &status)
}

// startServer runs `serve` for the disk in a session of its own, so it
// outlives the attach that starts it, and waits until it listens.
func startServer(ctx context.Context, p diskPaths, state *diskState, storePath string) error {
	if serverAlive(p, state.ServerPID) {
		return nil
	}
	self, err := os.Executable()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(p.runDir(), 0o700); err != nil {
		return err
	}
	log, err := os.OpenFile(p.serveLog(), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer log.Close()
	storeAbs, err := filepath.Abs(storePath)
	if err != nil {
		return err
	}
	cmd := exec.Command(self, "serve", "--root", p.root, "--disk", p.id, "--store", storeAbs)
	cmd.Stdout = log
	cmd.Stderr = log
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start serving disk %s: %w", p.id, err)
	}
	state.ServerPID = cmd.Process.Pid
	cmd.Process.Release()
	if err := saveState(p, state); err != nil {
		return err
	}
	deadline := time.Now().Add(serverStartWait)
	for {
		conn, err := net.Dial("unix", p.layersSocket())
		if err == nil {
			conn.Close()
			return nil
		}
		if !serverAlive(p, state.ServerPID) {
			return fmt.Errorf("serving disk %s exited at start; see %s", p.id, p.serveLog())
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("serving disk %s did not listen within %s", p.id, serverStartWait)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(50 * time.Millisecond):
		}
	}
}

// serverAlive reports whether pid is this disk's server, by its command line,
// since a restarted machine may have given the pid to anything.
func serverAlive(p diskPaths, pid int) bool {
	if pid <= 0 {
		return false
	}
	cmdline, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "cmdline"))
	if err != nil {
		return false
	}
	args := strings.Split(strings.TrimRight(string(cmdline), "\x00"), "\x00")
	return slices.Contains(args, "serve") && slices.Contains(args, p.root) && slices.Contains(args, p.id)
}

// stopServer ends the server once nothing reads through it. It flushes its
// bitmaps on the way out.
func stopServer(ctx context.Context, p diskPaths, state *diskState) error {
	pid := state.ServerPID
	if serverAlive(p, pid) {
		if err := syscall.Kill(pid, syscall.SIGTERM); err != nil && !errors.Is(err, syscall.ESRCH) {
			return fmt.Errorf("stop serving disk %s: %w", p.id, err)
		}
		deadline := time.Now().Add(serverStopWait)
		for serverAlive(p, pid) {
			if time.Now().After(deadline) {
				return fmt.Errorf("serving disk %s (pid %d) did not exit within %s", p.id, pid, serverStopWait)
			}
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(50 * time.Millisecond):
			}
		}
	}
	state.ServerPID = 0
	return nil
}
