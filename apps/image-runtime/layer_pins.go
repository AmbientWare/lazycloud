package main

import (
	"errors"
	"os"
	"path/filepath"
	"sync"
	"syscall"

	clipcommon "github.com/beam-cloud/clip/pkg/common"
)

type imageLayerPins struct {
	files []*os.File
	once  sync.Once
}

func pinImageLayers(root string, metadata *clipcommon.ClipArchiveMetadata) (*imageLayerPins, error) {
	info, err := ociStorageInfo(metadata)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(root, 0o700); err != nil {
		return nil, err
	}
	pins := &imageLayerPins{}
	for _, hash := range info.DecompressedHashByLayer {
		normalized, err := normalizedCacheHash(hash)
		if err != nil {
			pins.close()
			return nil, err
		}
		if normalized != hash {
			pins.close()
			return nil, errors.New("image layer content hash must be an unprefixed SHA-256 digest")
		}
		file, err := os.OpenFile(filepath.Join(root, "."+hash+".pin"), os.O_CREATE|os.O_RDWR, 0o600)
		if err != nil {
			pins.close()
			return nil, err
		}
		pins.files = append(pins.files, file)
		if err := syscall.Flock(int(file.Fd()), syscall.LOCK_SH); err != nil {
			pins.close()
			return nil, err
		}
	}
	return pins, nil
}

func (pins *imageLayerPins) close() {
	pins.once.Do(func() {
		for _, file := range pins.files {
			file.Close()
		}
	})
}
