package images

import (
	"context"
	"crypto/sha256"
	"fmt"
	"time"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// RegisterFilesystem makes an image a host pushed from a container's
// filesystem at reference (repository@digest) an image of workspace, and
// starts the build with no steps that converts it; the image is ready once
// that build publishes. Its identity is the pushed digest, so registering it
// again returns the same id. python is the Python minor version of the
// container's release.
func (i *Images) RegisterFilesystem(ctx context.Context, workspace identity.WorkspaceID, reference, architecture, python string) (string, error) {
	sum := sha256.Sum256([]byte("filesystem\x00" + reference))
	p := prepared{
		spec:           spec{python: python, architecture: architecture},
		dockerfile:     "# The filesystem of a sandbox container, published as one layer.\nFROM " + reference + "\n",
		digest:         sum[:],
		id:             imageID(sum[:]),
		secretVersions: map[string]string{},
	}
	r, err := i.build(ctx, workspace, p, false, buildOwnMirror)
	if err != nil {
		return "", fmt.Errorf("register filesystem image: %w", err)
	}
	return r.Image.ID, nil
}

// FilesystemRepository is where a host pushes a workspace's filesystem
// images.
func (i *Images) FilesystemRepository(workspace identity.WorkspaceID) string {
	return i.config.Registry + "/" + i.config.filesystemRepository(workspace)
}

// FilesystemTarget is FilesystemRepository, whether the registry speaks
// plain HTTP, and a login that may push there and nowhere else until
// deadline.
func (i *Images) FilesystemTarget(ctx context.Context, workspace identity.WorkspaceID, deadline time.Time) (repository string, insecure bool, auth *Auth, err error) {
	access := hostAccess{push: []string{i.config.filesystemRepository(workspace)}}
	if auth, err = i.login.host(ctx, access, deadline.Add(time.Minute)); err != nil {
		return "", false, nil, err
	}
	return i.FilesystemRepository(workspace), i.config.Insecure, auth, nil
}
