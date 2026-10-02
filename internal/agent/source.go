package agent

import (
	"archive/zip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"time"

	cerrdefs "github.com/containerd/errdefs"
	"github.com/moby/moby/client"
	"golang.org/x/sync/singleflight"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	// maxSourceBytes bounds a downloaded source archive.
	maxSourceBytes = 1 << 30
	// maxWorkspaceBytes and maxWorkspaceFiles bound an extracted workspace.
	maxWorkspaceBytes = 4 << 30
	maxWorkspaceFiles = 100_000
)

var sha256Hex = regexp.MustCompile(`^[0-9a-f]{64}$`)

// sourceCache keeps verified source archives by digest. Concurrent starts of
// the same digest share one download; a finished archive is renamed into
// place, so a partial download is never used.
type sourceCache struct {
	dir   string
	http  *http.Client
	group singleflight.Group
}

// fetch returns the path of the verified archive for src.
func (s *sourceCache) fetch(ctx context.Context, src *hostproto.Source) (string, error) {
	digest := src.GetSha256()
	if !sha256Hex.MatchString(digest) {
		return "", fmt.Errorf("source digest %q is not a lowercase SHA-256", digest)
	}
	path := filepath.Join(s.dir, digest+".zip")
	for {
		_, err, _ := s.group.Do(digest, func() (any, error) {
			if _, err := os.Stat(path); err == nil {
				return nil, nil
			}
			return nil, s.download(ctx, src.GetUrl(), digest, path)
		})
		// Another start's cancellation must not fail this one.
		if errors.Is(err, context.Canceled) && ctx.Err() == nil {
			continue
		}
		if err != nil {
			return "", fmt.Errorf("fetch source %s: %w", digest, err)
		}
		now := time.Now()
		_ = os.Chtimes(path, now, now)
		return path, nil
	}
}

func (s *sourceCache) download(ctx context.Context, url, digest, path string) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return fmt.Errorf("source URL: %w", err)
	}
	response, err := s.http.Do(request)
	if err != nil {
		return fmt.Errorf("download source: %w", err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("download source: HTTP %d", response.StatusCode)
	}
	tmp, err := os.CreateTemp(s.dir, digest+".*.tmp")
	if err != nil {
		return fmt.Errorf("create source file: %w", err)
	}
	defer func() { _ = os.Remove(tmp.Name()) }()
	hash := sha256.New()
	n, err := io.Copy(io.MultiWriter(tmp, hash), io.LimitReader(response.Body, maxSourceBytes+1))
	if closeErr := tmp.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return fmt.Errorf("download source: %w", err)
	}
	if n > maxSourceBytes {
		return fmt.Errorf("source archive exceeds %d bytes", maxSourceBytes)
	}
	if got := hex.EncodeToString(hash.Sum(nil)); got != digest {
		return fmt.Errorf("source digest mismatch: got %s, want %s", got, digest)
	}
	if err := os.Rename(tmp.Name(), path); err != nil {
		return fmt.Errorf("store source: %w", err)
	}
	return nil
}

// extractWorkspace unpacks archive into a new directory at dir. Entries are
// opened through an os.Root, so no name or link can escape it; symlinks are
// rejected outright.
func extractWorkspace(archive, dir string) error {
	if err := os.RemoveAll(dir); err != nil {
		return fmt.Errorf("clear workspace: %w", err)
	}
	if err := os.MkdirAll(dir, 0o755); err != nil { //nolint:gosec // the container reads its workspace
		return fmt.Errorf("create workspace: %w", err)
	}
	reader, err := zip.OpenReader(archive)
	if err != nil {
		return fmt.Errorf("open source archive: %w", err)
	}
	defer func() { _ = reader.Close() }()
	if len(reader.File) > maxWorkspaceFiles {
		return fmt.Errorf("source archive has more than %d entries", maxWorkspaceFiles)
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		return fmt.Errorf("open workspace: %w", err)
	}
	defer func() { _ = root.Close() }()
	var total int64
	for _, entry := range reader.File {
		name := filepath.FromSlash(entry.Name)
		if !filepath.IsLocal(name) {
			return fmt.Errorf("source archive entry %q escapes the workspace", entry.Name)
		}
		mode := entry.Mode()
		switch {
		case mode.IsDir():
			if err := root.MkdirAll(name, 0o755); err != nil {
				return fmt.Errorf("extract %s: %w", entry.Name, err)
			}
			continue
		case !mode.IsRegular():
			return fmt.Errorf("source archive entry %q is not a regular file", entry.Name)
		}
		if parent := filepath.Dir(name); parent != "." {
			if err := root.MkdirAll(parent, 0o755); err != nil {
				return fmt.Errorf("extract %s: %w", entry.Name, err)
			}
		}
		written, err := extractFile(root, name, entry, maxWorkspaceBytes-total)
		if err != nil {
			return fmt.Errorf("extract %s: %w", entry.Name, err)
		}
		total += written
	}
	return nil
}

func extractFile(root *os.Root, name string, entry *zip.File, budget int64) (int64, error) {
	perm := fs.FileMode(0o644)
	if entry.Mode()&0o111 != 0 {
		perm = 0o755
	}
	src, err := entry.Open()
	if err != nil {
		return 0, err //nolint:wrapcheck // extractWorkspace adds the entry name
	}
	defer func() { _ = src.Close() }()
	dst, err := root.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_EXCL, perm)
	if err != nil {
		return 0, err //nolint:wrapcheck // extractWorkspace adds the entry name
	}
	n, err := io.Copy(dst, io.LimitReader(src, budget+1))
	if closeErr := dst.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return n, err //nolint:wrapcheck // extractWorkspace adds the entry name
	}
	if n > budget {
		return n, fmt.Errorf("workspace exceeds %d bytes", maxWorkspaceBytes)
	}
	return n, nil
}

// imageCache pulls each image reference once at a time.
type imageCache struct {
	docker *client.Client
	group  singleflight.Group
}

// ensure makes image present and reports whether it had to be pulled. auth
// and platform go to the pull only; nothing stores the credentials.
func (c *imageCache) ensure(ctx context.Context, image string, auth *hostproto.RegistryAuth, platform string) (bool, error) {
	options, err := pullOptions(auth, platform)
	if err != nil {
		return false, err
	}
	for {
		pulled, err, _ := c.group.Do(image, func() (any, error) {
			if _, err := c.docker.ImageInspect(ctx, image); err == nil {
				return false, nil
			} else if !cerrdefs.IsNotFound(err) {
				return false, fmt.Errorf("inspect image %s: %w", image, err)
			}
			response, err := c.docker.ImagePull(ctx, image, options)
			if err != nil {
				return false, fmt.Errorf("pull image %s: %w", image, err)
			}
			defer func() { _ = response.Close() }()
			if err := response.Wait(ctx); err != nil {
				return false, fmt.Errorf("pull image %s: %w", image, err)
			}
			return true, nil
		})
		if errors.Is(err, context.Canceled) && ctx.Err() == nil {
			continue
		}
		if err != nil {
			return false, err //nolint:wrapcheck // wrapped inside the call
		}
		return pulled.(bool), nil //nolint:forcetypeassert // the function returns bool
	}
}
