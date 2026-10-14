// Package imagefstest gives tests registry images by pinned digest from an
// OCI image layout cache that host and test VM share and runs keep, so a
// run reads a registry only for an image the cache lacks.
package imagefstest

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/google/go-containerregistry/pkg/name"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/empty"
	"github.com/google/go-containerregistry/pkg/v1/layout"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"golang.org/x/sys/unix"
)

// CacheDir is the image cache, relative to the main checkout, which the
// test VM mounts at the same path and CI caches between runs.
const CacheDir = ".lazycloud/test-images"

// refName is the annotation the cache keys an image by: its pinned
// reference and platform.
const refName = "org.opencontainers.image.ref.name"

// Image returns reference, which names its digest, for platform: from the
// cache, or from its registry once and then from the cache.
func Image(ctx context.Context, reference string, platform v1.Platform) (v1.Image, error) {
	ref, err := name.ParseReference(reference)
	if err != nil {
		return nil, fmt.Errorf("parse %s: %w", reference, err)
	}
	if _, pinned := ref.(name.Digest); !pinned {
		return nil, fmt.Errorf("%s names no digest; test images are pinned", reference)
	}
	dir, err := cacheDir()
	if err != nil {
		return nil, err
	}
	unlock, err := lock(dir)
	if err != nil {
		return nil, err
	}
	defer unlock()
	cache, err := layout.FromPath(dir)
	if errors.Is(err, os.ErrNotExist) {
		cache, err = layout.Write(dir, empty.Index)
	}
	if err != nil {
		return nil, fmt.Errorf("open the image cache %s: %w", dir, err)
	}
	key := reference + "@" + platform.String()
	index, err := cache.ImageIndex()
	if err != nil {
		return nil, fmt.Errorf("read the image cache: %w", err)
	}
	manifest, err := index.IndexManifest()
	if err != nil {
		return nil, fmt.Errorf("read the image cache: %w", err)
	}
	for _, desc := range manifest.Manifests {
		if desc.Annotations[refName] == key {
			img, err := cache.Image(desc.Digest)
			if err != nil {
				return nil, fmt.Errorf("read %s from the image cache: %w", reference, err)
			}
			return img, nil
		}
	}
	img, err := remote.Image(ref, remote.WithContext(ctx), remote.WithPlatform(platform))
	if err != nil {
		return nil, fmt.Errorf("pull %s: %w", reference, err)
	}
	if err := cache.AppendImage(img, layout.WithAnnotations(map[string]string{refName: key})); err != nil {
		return nil, fmt.Errorf("cache %s: %w", reference, err)
	}
	digest, err := img.Digest()
	if err != nil {
		return nil, fmt.Errorf("cache %s: %w", reference, err)
	}
	cached, err := cache.Image(digest)
	if err != nil {
		return nil, fmt.Errorf("read %s from the image cache: %w", reference, err)
	}
	return cached, nil
}

// lock holds the cache for one process's lookup and append, which
// parallel tests and packages make at once.
func lock(dir string) (func(), error) {
	if err := os.MkdirAll(dir, 0o777); err != nil { //nolint:gosec // root in the test VM and the host's user both write it.
		return nil, fmt.Errorf("create the image cache: %w", err)
	}
	f, err := os.OpenFile(filepath.Join(dir, ".lock"), os.O_CREATE|os.O_RDWR, 0o666) //nolint:gosec // as above
	if err != nil {
		return nil, fmt.Errorf("lock the image cache: %w", err)
	}
	if err := unix.Flock(int(f.Fd()), unix.LOCK_EX); err != nil {
		_ = f.Close()
		return nil, fmt.Errorf("lock the image cache: %w", err)
	}
	return func() { _ = f.Close() }, nil
}

// cacheDir is CacheDir in the main checkout of the working directory's
// tree: a worktree's .git file names the main checkout's .git directory.
func cacheDir() (string, error) {
	dir, err := os.Getwd()
	if err != nil {
		return "", fmt.Errorf("find the checkout: %w", err)
	}
	for {
		dotGit := filepath.Join(dir, ".git")
		info, err := os.Stat(dotGit)
		if err == nil && info.IsDir() {
			return filepath.Join(dir, CacheDir), nil
		}
		if err == nil {
			data, err := os.ReadFile(dotGit) //nolint:gosec // the checkout's own file
			if err != nil {
				return "", fmt.Errorf("find the checkout: %w", err)
			}
			gitDir := strings.TrimSpace(strings.TrimPrefix(string(data), "gitdir:"))
			main, _, found := strings.Cut(gitDir, string(filepath.Separator)+".git"+string(filepath.Separator))
			if !found {
				return "", fmt.Errorf("%s names no main checkout: %s", dotGit, gitDir)
			}
			return filepath.Join(main, CacheDir), nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", errors.New("the tests run outside a checkout")
		}
		dir = parent
	}
}
