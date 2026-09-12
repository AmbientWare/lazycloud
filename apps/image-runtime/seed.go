package main

import (
	"errors"
	"fmt"

	"github.com/beam-cloud/clip/pkg/clip"
	"github.com/google/go-containerregistry/pkg/v1/layout"
	"golang.org/x/sync/errgroup"
)

func seedImageLayers(layoutPath, archivePath string, cache *httpContentCache) error {
	metadata, err := clip.NewClipArchiver().ExtractMetadata(archivePath)
	if err != nil {
		return err
	}
	info, err := ociStorageInfo(metadata)
	if err != nil {
		return err
	}
	index, err := layout.ImageIndexFromPath(layoutPath)
	if err != nil {
		return err
	}
	manifest, err := index.IndexManifest()
	if err != nil {
		return err
	}
	if len(manifest.Manifests) != 1 {
		return errors.New("image build layout must contain exactly one image")
	}
	image, err := index.Image(manifest.Manifests[0].Digest)
	if err != nil {
		return err
	}
	layers, err := image.Layers()
	if err != nil {
		return err
	}
	var group errgroup.Group
	group.SetLimit(8)
	for _, layer := range layers {
		group.Go(func() error {
			digest, err := layer.Digest()
			if err != nil {
				return err
			}
			hash := info.DecompressedHashByLayer[digest.String()]
			if hash == "" {
				return errors.New("indexed layer has no verified content hash")
			}
			opts := struct{ RoutingKey string }{RoutingKey: hash}
			present, err := cache.ContentExists(hash, opts)
			if err != nil || present {
				return err
			}
			stream, err := layer.Uncompressed()
			if err != nil {
				return err
			}
			defer stream.Close()
			_, err = cache.store(stream, hash, -1)
			return err
		})
	}
	if err := group.Wait(); err != nil {
		return fmt.Errorf("image layer cache population failed: %w", err)
	}
	return nil
}
