package imagefstest

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"

	"github.com/google/go-containerregistry/pkg/name"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/go-containerregistry/pkg/v1/tarball"
)

// Load gives Docker reference for platform from the cache and returns the
// tag Docker holds it under, so a run never pulls it from its registry.
func Load(ctx context.Context, reference string, platform v1.Platform) (string, error) {
	ref, err := name.ParseReference(reference)
	if err != nil {
		return "", fmt.Errorf("parse %s: %w", reference, err)
	}
	digest, _ := ref.(name.Digest)
	repository := ref.Context().RepositoryStr()
	tag := "lazycloud-test/" + repository[strings.LastIndex(repository, "/")+1:] + ":" + strings.TrimPrefix(digest.DigestStr(), "sha256:")[:12]
	if exec.CommandContext(ctx, "docker", "image", "inspect", tag).Run() == nil { //nolint:gosec // The tag derives from a pinned reference.
		return tag, nil
	}
	img, err := Image(ctx, reference, platform)
	if err != nil {
		return "", err
	}
	tagged, err := name.NewTag(tag)
	if err != nil {
		return "", fmt.Errorf("tag %s: %w", tag, err)
	}
	var archive bytes.Buffer
	if err := tarball.Write(tagged, img, &archive); err != nil {
		return "", fmt.Errorf("archive %s: %w", reference, err)
	}
	load := exec.CommandContext(ctx, "docker", "load", "--quiet")
	load.Stdin = &archive
	if out, err := load.CombinedOutput(); err != nil {
		return "", fmt.Errorf("load %s into Docker: %w: %s", reference, err, out)
	}
	return tag, nil
}

// Push copies reference for platform from the cache into registry, a
// loopback registry over plain HTTP, under the same repository path and
// digest, and returns the copy's reference.
func Push(ctx context.Context, registry, reference string, platform v1.Platform) (string, error) {
	img, err := Image(ctx, reference, platform)
	if err != nil {
		return "", err
	}
	ref, err := name.ParseReference(reference)
	if err != nil {
		return "", fmt.Errorf("parse %s: %w", reference, err)
	}
	digest, err := img.Digest()
	if err != nil {
		return "", fmt.Errorf("digest %s: %w", reference, err)
	}
	copied, err := name.NewDigest(registry+"/"+ref.Context().RepositoryStr()+"@"+digest.String(), name.Insecure)
	if err != nil {
		return "", fmt.Errorf("name the copy of %s: %w", reference, err)
	}
	if err := remote.Write(copied, img, remote.WithContext(ctx)); err != nil {
		return "", fmt.Errorf("copy %s to %s: %w", reference, registry, err)
	}
	return copied.String(), nil
}
