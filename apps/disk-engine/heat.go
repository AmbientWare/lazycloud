package main

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"slices"
	"unsafe"

	"golang.org/x/sys/unix"
)

// The heat map records, per region of the base layer file, how many sampling
// windows ago the region was last read or written. A snapshot carries it, and
// hydrating a volume made from that snapshot reads the regions in that order.
const (
	heatMinShift   = 20 // 1 MiB regions
	heatMaxRegions = 1 << 17
	heatCold       = 255
	heatMagic      = "LCHEAT01"
	// residencyWindow bounds each mapping mincore reads, so sampling a 1 TiB
	// layer never maps more than this at once.
	residencyWindow = 1 << 30
)

type heatMap struct {
	baseSeq int
	shift   uint
	ages    []byte
}

// heatShiftFor is the region size, as a power of two, that keeps a file of
// size bytes within heatMaxRegions.
func heatShiftFor(size int64) uint {
	shift := uint(heatMinShift)
	for regionCount(size, shift) > heatMaxRegions {
		shift++
	}
	return shift
}

func regionCount(size int64, shift uint) int {
	return int((size + (int64(1) << shift) - 1) >> shift)
}

func newHeatMap(baseSeq int) *heatMap {
	return &heatMap{baseSeq: baseSeq, shift: heatMinShift}
}

// fit resizes the map for a base file of size bytes. A file that grew past
// heatMaxRegions regions doubles the region size, and a merged region keeps
// the more recent of its two ages.
func (h *heatMap) fit(size int64) {
	for want := heatShiftFor(size); h.shift < want; h.shift++ {
		merged := make([]byte, (len(h.ages)+1)/2)
		for i := range merged {
			age := h.ages[2*i]
			if 2*i+1 < len(h.ages) {
				age = min(age, h.ages[2*i+1])
			}
			merged[i] = age
		}
		h.ages = merged
	}
	for count := regionCount(size, h.shift); len(h.ages) < count; {
		h.ages = append(h.ages, heatCold)
	}
}

// age moves every region one window further into the past.
func (h *heatMap) age() {
	for i, age := range h.ages {
		if age < heatCold {
			h.ages[i] = age + 1
		}
	}
}

func (h *heatMap) touch(offset int64) {
	if region := int(offset >> h.shift); region < len(h.ages) {
		h.ages[region] = 0
	}
}

// hot lists every region touched within the map's memory, most recent first
// and in file order within one window.
func (h *heatMap) hot() []int {
	var regions []int
	for i, age := range h.ages {
		if age < heatCold {
			regions = append(regions, i)
		}
	}
	slices.SortStableFunc(regions, func(a, b int) int { return int(h.ages[a]) - int(h.ages[b]) })
	return regions
}

func (h *heatMap) regionBytes() int64 { return int64(1) << h.shift }

func (h *heatMap) encode() ([]byte, error) {
	var out bytes.Buffer
	writer := gzip.NewWriter(&out)
	header := make([]byte, 0, len(heatMagic)+12)
	header = append(header, heatMagic...)
	header = binary.BigEndian.AppendUint32(header, uint32(h.baseSeq))
	header = binary.BigEndian.AppendUint32(header, uint32(h.shift))
	header = binary.BigEndian.AppendUint32(header, uint32(len(h.ages)))
	if _, err := writer.Write(header); err != nil {
		return nil, err
	}
	if _, err := writer.Write(h.ages); err != nil {
		return nil, err
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	return out.Bytes(), nil
}

func decodeHeat(data []byte) (*heatMap, error) {
	reader, err := gzip.NewReader(bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	raw, err := io.ReadAll(io.LimitReader(reader, int64(len(heatMagic)+12+heatMaxRegions+1)))
	if err != nil {
		return nil, err
	}
	if len(raw) < len(heatMagic)+12 || string(raw[:len(heatMagic)]) != heatMagic {
		return nil, errors.New("not a heat map")
	}
	fields := raw[len(heatMagic):]
	h := &heatMap{
		baseSeq: int(binary.BigEndian.Uint32(fields[0:4])),
		shift:   uint(binary.BigEndian.Uint32(fields[4:8])),
	}
	count := int(binary.BigEndian.Uint32(fields[8:12]))
	ages := fields[12:]
	if h.shift < heatMinShift || h.shift > 62 || count > heatMaxRegions || len(ages) != count {
		return nil, fmt.Errorf("heat map holds %d regions of 2^%d bytes in %d bytes", count, h.shift, len(ages))
	}
	h.ages = slices.Clone(ages)
	return h, nil
}

// loadHeat returns nil when the disk has no heat map yet.
func loadHeat(p diskPaths) (*heatMap, error) {
	data, err := os.ReadFile(p.heatPath())
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	h, err := decodeHeat(data)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", p.heatPath(), err)
	}
	return h, nil
}

type heatResult struct {
	Regions     int   `json:"regions"`
	RegionBytes int64 `json:"region_bytes"`
	Touched     int   `json:"touched"`
	Hot         int   `json:"hot"`
}

// runHeat closes one sampling window. Every region ages by one, the regions of
// the base layer the page cache holds are marked touched, and the base's
// cached pages are dropped so the next window counts only its own reads and
// writes. Dirty pages survive the drop and count again next window, which
// only ever errs toward hot.
func runHeat(ctx context.Context, args []string) (any, error) {
	f := newFlags("heat", true)
	if err := f.parse(args); err != nil {
		return nil, err
	}
	p := f.paths()
	lock, err := lockDisk(p)
	if err != nil {
		return nil, err
	}
	defer lock.release()
	state, err := requireState(p)
	if err != nil {
		return nil, err
	}
	base := state.Layers[0]
	file, err := os.Open(p.layerPath(base))
	if err != nil {
		return nil, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, err
	}
	heat, err := loadHeat(p)
	if err != nil {
		return nil, err
	}
	if heat == nil || heat.baseSeq != base.Seq {
		heat = newHeatMap(base.Seq)
	}
	heat.fit(info.Size())
	heat.age()
	touched := map[int]bool{}
	err = residentPages(file, info.Size(), func(offset int64) {
		heat.touch(offset)
		touched[int(offset>>heat.shift)] = true
	})
	if err != nil {
		return nil, err
	}
	dropCache(file)
	data, err := heat.encode()
	if err != nil {
		return nil, err
	}
	if err := writeFileAtomic(p.heatPath(), data); err != nil {
		return nil, err
	}
	return heatResult{
		Regions: len(heat.ages), RegionBytes: heat.regionBytes(),
		Touched: len(touched), Hot: len(heat.hot()),
	}, nil
}

// residentPages calls visit with the offset of every page of file that is in
// the page cache.
func residentPages(file *os.File, size int64, visit func(offset int64)) error {
	page := int64(os.Getpagesize())
	for offset := int64(0); offset < size; offset += residencyWindow {
		length := min(int64(residencyWindow), size-offset)
		mapped, err := unix.Mmap(int(file.Fd()), offset, int(length), unix.PROT_READ, unix.MAP_SHARED)
		if err != nil {
			return fmt.Errorf("map %s at %d: %w", file.Name(), offset, err)
		}
		resident := make([]byte, (length+page-1)/page)
		err = mincore(mapped, resident)
		unmapErr := unix.Munmap(mapped)
		if err != nil {
			return fmt.Errorf("mincore %s at %d: %w", file.Name(), offset, err)
		}
		if unmapErr != nil {
			return unmapErr
		}
		for i, flags := range resident {
			if flags&1 != 0 {
				visit(offset + int64(i)*page)
			}
		}
	}
	return nil
}

// mincore fills resident with one byte per page of mapped, whose lowest bit
// says whether the page is in the page cache. x/sys/unix has no wrapper for
// it on Linux.
func mincore(mapped, resident []byte) error {
	_, _, errno := unix.Syscall(unix.SYS_MINCORE, uintptr(unsafe.Pointer(&mapped[0])), uintptr(len(mapped)), uintptr(unsafe.Pointer(&resident[0])))
	if errno != 0 {
		return errno
	}
	return nil
}

// dropCache evicts file's clean cached pages. Reads made on the workload's
// behalf are what the heat map counts, so anything the engine itself reads in
// bulk is dropped again.
func dropCache(file *os.File) {
	unix.Fadvise(int(file.Fd()), 0, 0, unix.FADV_DONTNEED)
}

func dropCachePath(path string) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	dropCache(file)
	return nil
}
