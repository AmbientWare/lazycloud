package main

import (
	"bytes"
	"compress/gzip"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"slices"
	"sync"
)

// The heat map names the chunks a disk's workload read, each with how many
// windows ago it last did, so the next restore fetches them first. It keys
// chunks by their digest, not their place, because chunks are content-addressed:
// the same data keeps its digest in whatever chain the next restore gets.
const (
	heatKeyBytes   = 8
	heatMaxEntries = 1 << 16
	heatCold       = 255
	heatMagic      = "LCHEAT02"
)

func diskHeatKey(diskID string) string {
	return fmt.Sprintf("%s/%s/heat", diskObjectPrefix, diskID)
}

type heatKey [heatKeyBytes]byte

func heatKeyOf(sum string) (heatKey, bool) {
	var key heatKey
	raw, err := hex.DecodeString(sum)
	if err != nil || len(raw) < heatKeyBytes {
		return key, false
	}
	copy(key[:], raw)
	return key, true
}

type heatMap struct {
	mu   sync.Mutex
	ages map[heatKey]byte
}

func newHeatMap() *heatMap { return &heatMap{ages: map[heatKey]byte{}} }

func (h *heatMap) touch(sum string) {
	key, ok := heatKeyOf(sum)
	if !ok {
		return
	}
	h.mu.Lock()
	h.ages[key] = 0
	h.mu.Unlock()
}

// age closes a window: every chunk moves one window into the past, and one
// untouched for as long as the map remembers is forgotten.
func (h *heatMap) age() {
	h.mu.Lock()
	defer h.mu.Unlock()
	for key, age := range h.ages {
		if age+1 >= heatCold {
			delete(h.ages, key)
		} else {
			h.ages[key] = age + 1
		}
	}
}

// ordered lists the chunks most recent first, and trims the map to its bound
// by dropping the oldest.
func (h *heatMap) ordered() []heatKey {
	h.mu.Lock()
	defer h.mu.Unlock()
	keys := make([]heatKey, 0, len(h.ages))
	for key := range h.ages {
		keys = append(keys, key)
	}
	slices.SortFunc(keys, func(a, b heatKey) int {
		if h.ages[a] != h.ages[b] {
			return int(h.ages[a]) - int(h.ages[b])
		}
		return bytes.Compare(a[:], b[:])
	})
	for _, key := range keys[min(len(keys), heatMaxEntries):] {
		delete(h.ages, key)
	}
	return keys[:min(len(keys), heatMaxEntries)]
}

func (h *heatMap) encode() ([]byte, error) {
	keys := h.ordered()
	var out bytes.Buffer
	writer := gzip.NewWriter(&out)
	header := binary.BigEndian.AppendUint32([]byte(heatMagic), uint32(len(keys)))
	if _, err := writer.Write(header); err != nil {
		return nil, err
	}
	h.mu.Lock()
	for _, key := range keys {
		writer.Write(key[:])
		writer.Write([]byte{h.ages[key]})
	}
	h.mu.Unlock()
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
	raw, err := io.ReadAll(io.LimitReader(reader, int64(len(heatMagic)+4+heatMaxEntries*(heatKeyBytes+1)+1)))
	if err != nil {
		return nil, err
	}
	if len(raw) < len(heatMagic)+4 || string(raw[:len(heatMagic)]) != heatMagic {
		return nil, errors.New("not a heat map")
	}
	count := int(binary.BigEndian.Uint32(raw[len(heatMagic):]))
	entries := raw[len(heatMagic)+4:]
	if count > heatMaxEntries || len(entries) != count*(heatKeyBytes+1) {
		return nil, fmt.Errorf("heat map claims %d entries in %d bytes", count, len(entries))
	}
	h := newHeatMap()
	for entry := range slices.Chunk(entries, heatKeyBytes+1) {
		var key heatKey
		copy(key[:], entry)
		h.ages[key] = entry[heatKeyBytes]
	}
	return h, nil
}

// loadHeat returns an empty map when the disk has none yet.
func loadHeat(p diskPaths) (*heatMap, error) {
	data, err := os.ReadFile(p.heatPath())
	if errors.Is(err, os.ErrNotExist) {
		return newHeatMap(), nil
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
