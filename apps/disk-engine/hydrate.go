package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
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
	"golang.org/x/sys/unix"
)

const (
	hydrateReadBytes = 1 << 20
	// hydrateAlign is the alignment O_DIRECT needs of offsets, lengths and
	// buffers on every device a volume attaches as.
	hydrateAlign       = 4096
	hydrateHotReaders  = 8
	hydrateColdReaders = 2
	// hydrateColdRate leaves most of a volume's provisioned throughput to the
	// workload while the blocks nobody has asked for yet load.
	hydrateColdRate  = 64 << 20
	hydratorStopWait = 10 * time.Second
)

type hydrateStatus struct {
	StartedAt      time.Time  `json:"started_at"`
	MetadataBytes  int64      `json:"metadata_bytes"`
	MetadataDoneAt *time.Time `json:"metadata_done_at,omitempty"`
	HotRegions     int        `json:"hot_regions"`
	HotBytes       int64      `json:"hot_bytes"`
	HotDoneAt      *time.Time `json:"hot_done_at,omitempty"`
	ColdBytes      int64      `json:"cold_bytes"`
	DoneAt         *time.Time `json:"done_at,omitempty"`
}

type extent struct{ offset, length int64 }

// runHydrate reads every block of a disk adopted from a snapshot once, so the
// provider loads each from the snapshot before the workload asks for it. The
// qcow2 tables come first, because every guest read needs one; then the
// regions the heat map saw used, most recent first; then everything else at
// hydrateColdRate. The workload's own reads never wait on this: the provider
// fetches a block the moment anything reads it.
//
// It takes no lock. It only reads published layers, which never change, and
// the base, which compaction only writes regions of.
func runHydrate(ctx context.Context, args []string) (any, error) {
	f := newFlags("hydrate", true)
	if err := f.parse(args); err != nil {
		return nil, err
	}
	p := f.paths()
	state, err := requireState(p)
	if err != nil {
		return nil, err
	}
	status := hydrateStatus{StartedAt: time.Now().UTC()}
	layers := state.Layers[:len(state.Layers)-1]
	files := make([]*os.File, len(layers))
	defer func() {
		for _, file := range files {
			if file != nil {
				file.Close()
			}
		}
	}()
	for i, l := range layers {
		if files[i], err = os.OpenFile(p.layerPath(l), os.O_RDONLY|unix.O_DIRECT, 0); err != nil {
			return nil, err
		}
	}

	for i, l := range layers {
		if l.Raw {
			continue
		}
		tables, err := qcow2Tables(p.layerPath(l))
		if err != nil {
			return nil, err
		}
		read, err := readExtents(ctx, files[i], tables, hydrateHotReaders, nil)
		status.MetadataBytes += read
		if err != nil {
			return nil, err
		}
	}
	status.MetadataDoneAt = timestamp()
	writeHydrateStatus(p, status)

	base := files[0]
	info, err := base.Stat()
	if err != nil {
		return nil, err
	}
	heat, err := loadHeat(p)
	if err != nil {
		return nil, err
	}
	var done *regionSet
	if heat != nil && heat.baseSeq == layers[0].Seq {
		heat.fit(info.Size())
		done = &regionSet{shift: heat.shift, read: make([]bool, len(heat.ages))}
		var regions []extent
		for _, region := range heat.hot() {
			start := int64(region) << heat.shift
			if start >= info.Size() {
				continue
			}
			regions = append(regions, extent{start, min(heat.regionBytes(), info.Size()-start)})
			done.read[region] = true
		}
		status.HotRegions = len(regions)
		read, err := readExtents(ctx, base, regions, hydrateHotReaders, nil)
		status.HotBytes = read
		if err != nil {
			return nil, err
		}
	}
	status.HotDoneAt = timestamp()
	writeHydrateStatus(p, status)

	limit := newRateLimit(hydrateColdRate)
	for i, file := range files {
		info, err := file.Stat()
		if err != nil {
			return nil, err
		}
		runs, err := fileRuns(file, info.Size())
		if err != nil {
			return nil, err
		}
		var cold []extent
		for _, run := range runs {
			cold = append(cold, extent{run.start, run.length})
		}
		if i == 0 && done != nil {
			cold = outside(cold, done)
		}
		read, err := readExtents(ctx, file, cold, hydrateColdReaders, limit)
		status.ColdBytes += read
		if err != nil {
			return nil, err
		}
	}
	status.DoneAt = timestamp()
	writeHydrateStatus(p, status)
	return status, nil
}

// regionSet marks the heat map's regions the hot pass already read.
type regionSet struct {
	shift uint
	read  []bool
}

// outside cuts the regions marked in done out of extents.
func outside(extents []extent, done *regionSet) []extent {
	var kept []extent
	size := int64(1) << done.shift
	for _, e := range extents {
		for offset := e.offset; offset < e.offset+e.length; {
			region := int(offset >> done.shift)
			end := min((int64(region)+1)*size, e.offset+e.length)
			if region >= len(done.read) || !done.read[region] {
				if n := len(kept); n > 0 && kept[n-1].offset+kept[n-1].length == offset {
					kept[n-1].length += end - offset
				} else {
					kept = append(kept, extent{offset, end - offset})
				}
			}
			offset = end
		}
	}
	return kept
}

// readExtents reads each extent once, in order, and discards what it read.
func readExtents(ctx context.Context, file *os.File, extents []extent, readers int, limit *rateLimit) (int64, error) {
	type piece struct{ offset, length int64 }
	pieces := make(chan piece)
	var total int64
	var mu sync.Mutex
	group, groupCtx := errgroup.WithContext(ctx)
	for range readers {
		group.Go(func() error {
			buffer, err := unix.Mmap(-1, 0, hydrateReadBytes, unix.PROT_READ|unix.PROT_WRITE, unix.MAP_ANON|unix.MAP_PRIVATE)
			if err != nil {
				return err
			}
			defer unix.Munmap(buffer)
			for next := range pieces {
				if err := limit.wait(groupCtx, next.length); err != nil {
					return err
				}
				n, err := file.ReadAt(buffer[:next.length], next.offset)
				if err != nil && !errors.Is(err, io.EOF) {
					return fmt.Errorf("read %s at %d: %w", file.Name(), next.offset, err)
				}
				mu.Lock()
				total += int64(n)
				mu.Unlock()
			}
			return nil
		})
	}
	group.Go(func() error {
		defer close(pieces)
		for _, e := range extents {
			start := e.offset / hydrateAlign * hydrateAlign
			end := (e.offset + e.length + hydrateAlign - 1) / hydrateAlign * hydrateAlign
			for offset := start; offset < end; offset += hydrateReadBytes {
				select {
				case pieces <- piece{offset, min(hydrateReadBytes, end-offset)}:
				case <-groupCtx.Done():
					return groupCtx.Err()
				}
			}
		}
		return nil
	})
	err := group.Wait()
	return total, err
}

// qcow2Tables lists the parts of a qcow2 file every lookup reads: the header
// cluster, the L1 table and each L2 table it points to, and the refcount table
// and its blocks.
func qcow2Tables(path string) ([]extent, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	defer dropCache(file)
	header := make([]byte, 72)
	if _, err := io.ReadFull(file, header); err != nil {
		return nil, fmt.Errorf("read qcow2 header of %s: %w", path, err)
	}
	if string(header[:4]) != "QFI\xfb" {
		return nil, fmt.Errorf("%s is not a qcow2 image", path)
	}
	clusterBits := binary.BigEndian.Uint32(header[20:24])
	if clusterBits < 9 || clusterBits > 21 {
		return nil, fmt.Errorf("%s has %d-bit clusters", path, clusterBits)
	}
	cluster := int64(1) << clusterBits
	l1Entries := int64(binary.BigEndian.Uint32(header[36:40]))
	l1Offset := int64(binary.BigEndian.Uint64(header[40:48]))
	refcountOffset := int64(binary.BigEndian.Uint64(header[48:56]))
	refcountClusters := int64(binary.BigEndian.Uint32(header[56:60]))

	tables := []extent{{0, cluster}}
	pointers := func(offset, entries int64) error {
		if entries == 0 {
			return nil
		}
		tables = append(tables, extent{offset, entries * 8})
		raw := make([]byte, entries*8)
		if _, err := file.ReadAt(raw, offset); err != nil {
			return fmt.Errorf("read qcow2 table of %s at %d: %w", path, offset, err)
		}
		for i := int64(0); i < entries; i++ {
			if at := int64(binary.BigEndian.Uint64(raw[i*8:]) & 0x00fffffffffffe00); at != 0 {
				tables = append(tables, extent{at, cluster})
			}
		}
		return nil
	}
	if err := pointers(l1Offset, l1Entries); err != nil {
		return nil, err
	}
	if err := pointers(refcountOffset, refcountClusters*cluster/8); err != nil {
		return nil, err
	}
	slices.SortFunc(tables, func(a, b extent) int { return int(a.offset - b.offset) })
	return tables, nil
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
		return nil
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

func timestamp() *time.Time {
	now := time.Now().UTC()
	return &now
}

// writeHydrateStatus records progress for whoever looks; a hydration that
// cannot record it still hydrates.
func writeHydrateStatus(p diskPaths, status hydrateStatus) {
	data, err := json.Marshal(status)
	if err == nil && os.MkdirAll(p.runDir(), 0o700) == nil {
		writeFileAtomic(p.hydrateStatus(), data)
	}
}

// hydrated reports whether a hydrator finished reading this disk.
func hydrated(p diskPaths) bool {
	var status hydrateStatus
	data, err := os.ReadFile(p.hydrateStatus())
	return err == nil && json.Unmarshal(data, &status) == nil && status.DoneAt != nil
}

// startHydrator runs `hydrate` for the disk in a session of its own, so it
// outlives the attach that starts it, and records its pid.
func startHydrator(p diskPaths, state *diskState) error {
	if hydrated(p) {
		state.Hydrating = false
		state.HydratorPID = 0
		return saveState(p, state)
	}
	if hydratorAlive(p, state.HydratorPID) {
		return nil
	}
	self, err := os.Executable()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(p.runDir(), 0o700); err != nil {
		return err
	}
	log, err := os.OpenFile(p.hydrateLog(), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer log.Close()
	cmd := exec.Command(self, "hydrate", "--root", p.root, "--disk", p.id)
	cmd.Stdout = log
	cmd.Stderr = log
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start hydrating disk %s: %w", p.id, err)
	}
	state.HydratorPID = cmd.Process.Pid
	cmd.Process.Release()
	return saveState(p, state)
}

// hydratorAlive reports whether pid is this disk's hydrator, by its command
// line, since a restarted machine may have given the pid to anything.
func hydratorAlive(p diskPaths, pid int) bool {
	if pid <= 0 {
		return false
	}
	cmdline, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "cmdline"))
	if err != nil {
		return false
	}
	args := strings.Split(strings.TrimRight(string(cmdline), "\x00"), "\x00")
	return slices.Contains(args, "hydrate") && slices.Contains(args, p.root) && slices.Contains(args, p.id)
}

// stopHydrator ends a running hydrator, which holds the disk's layer files
// open and so would keep its volume from unmounting.
func stopHydrator(ctx context.Context, p diskPaths, state *diskState) error {
	pid := state.HydratorPID
	if hydratorAlive(p, pid) {
		if err := syscall.Kill(pid, syscall.SIGTERM); err != nil && !errors.Is(err, syscall.ESRCH) {
			return fmt.Errorf("stop hydrator %d of disk %s: %w", pid, p.id, err)
		}
		deadline := time.Now().Add(hydratorStopWait)
		for hydratorAlive(p, pid) {
			if time.Now().After(deadline) {
				syscall.Kill(pid, syscall.SIGKILL)
				break
			}
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(50 * time.Millisecond):
			}
		}
	}
	state.HydratorPID = 0
	if hydrated(p) {
		state.Hydrating = false
	}
	return nil
}
