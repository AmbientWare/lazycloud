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
	"maps"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"time"

	containerd "github.com/containerd/containerd/v2/client"
	"github.com/containerd/containerd/v2/core/images"
	"github.com/containerd/containerd/v2/core/remotes"
	"github.com/containerd/containerd/v2/core/remotes/docker"
	cerrdefs "github.com/containerd/errdefs"
	"github.com/containerd/platforms"
	"github.com/distribution/reference"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"golang.org/x/sync/singleflight"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
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
	// containerd is Docker's containerd, which every pull goes through.
	containerd *containerd.Client
	group      singleflight.Group
}

// ensureLazy makes a converted image present through containerd in
// Docker's namespace and reports whether it had to pull. Every layer is
// marked lazy, so the snapshotter mounts it from the layer grants the agent
// gave it and the registry serves only the manifest and config. A layer
// without a live grant fails the pull. Docker then runs the image as any
// other.
func (c *imageCache) ensureLazy(ctx context.Context, image string, auth *hostproto.RegistryAuth, platform string) (bool, error) {
	named, err := reference.ParseDockerRef(image)
	if err != nil {
		return false, fmt.Errorf("image reference %q: %w", image, err)
	}
	ref := named.String()
	matcher := platforms.Default()
	opts := []containerd.RemoteOpt{
		containerd.WithPullUnpack,
		containerd.WithPullSnapshotter(layersource.Snapshotter),
		containerd.WithResolver(registryResolver(reference.Domain(named), auth)),
		containerd.WithImageHandlerWrapper(markLayersLazy),
	}
	if platform != "" {
		p, err := platforms.Parse(platform)
		if err != nil {
			return false, fmt.Errorf("image platform %q: %w", platform, err)
		}
		matcher = platforms.Only(p)
		opts = append(opts, containerd.WithPlatform(platform))
	}
	for {
		pulled, err, _ := c.group.Do("lazy "+ref, func() (any, error) {
			if img, err := c.containerd.GetImage(ctx, ref); err == nil {
				unpacked, err := containerd.NewImageWithPlatform(c.containerd, img.Metadata(), matcher).IsUnpacked(ctx, layersource.Snapshotter)
				if err != nil {
					return false, fmt.Errorf("inspect image %s: %w", ref, err)
				}
				if unpacked {
					return false, nil
				}
			} else if !cerrdefs.IsNotFound(err) {
				return false, fmt.Errorf("inspect image %s: %w", ref, err)
			}
			if _, err := c.containerd.Pull(ctx, ref, opts...); err != nil {
				return false, fmt.Errorf("pull image %s: %w", ref, err)
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

// markLayersLazy labels each layer of a manifest for the snapshotter.
func markLayersLazy(h images.Handler) images.Handler {
	return images.HandlerFunc(func(ctx context.Context, desc ocispec.Descriptor) ([]ocispec.Descriptor, error) {
		children, err := h.Handle(ctx, desc)
		if err != nil || !images.IsManifestType(desc.MediaType) {
			return children, err //nolint:wrapcheck // containerd's handler error
		}
		for i := range children {
			if images.IsLayerType(children[i].MediaType) {
				annotations := maps.Clone(children[i].Annotations)
				if annotations == nil {
					annotations = make(map[string]string, 1)
				}
				annotations[layersource.LazyLabel] = "true"
				children[i].Annotations = annotations
			}
		}
		return children, nil
	})
}

// registryResolver resolves through the image's registry with its login,
// sent to that registry only; loopback registries are plain HTTP.
func registryResolver(domain string, auth *hostproto.RegistryAuth) remotes.Resolver {
	hosts := map[string]bool{domain: true}
	if domain == "docker.io" {
		hosts["registry-1.docker.io"] = true
	}
	creds := func(host string) (string, string, error) {
		if auth == nil || !hosts[host] {
			return "", "", nil
		}
		if token := auth.GetIdentityToken(); token != "" {
			return "", token, nil
		}
		return auth.GetUsername(), auth.GetPassword(), nil
	}
	return docker.NewResolver(docker.ResolverOptions{Hosts: docker.ConfigureDefaultRegistries(
		docker.WithAuthorizer(docker.NewDockerAuthorizer(docker.WithAuthCreds(creds))),
		docker.WithPlainHTTP(docker.MatchLocalhost),
	)})
}
