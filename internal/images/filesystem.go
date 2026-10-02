package images

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"

	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// RegisterFilesystem makes an image a host pushed from a container's
// filesystem an image of workspace, ready at reference (repository@digest).
// Its identity is the pushed digest, so registering it again returns the
// same id. python is the Python minor version of the container's release.
func (i *Images) RegisterFilesystem(ctx context.Context, workspace identity.WorkspaceID, reference, architecture, python string) (string, error) {
	sum := sha256.Sum256([]byte("filesystem\x00" + reference))
	p := prepared{
		spec:       spec{python: python, architecture: architecture},
		dockerfile: "# The filesystem of a sandbox container, published as one layer.\nFROM " + reference + "\n",
		digest:     sum[:],
		id:         "img_" + hex.EncodeToString(sum[:])[:24],
		reference:  &reference,
	}
	var id string
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		image, err := i.upsert(ctx, i.queries.WithTx(tx), workspace, p)
		id = image.ID
		return err
	})
	if err != nil {
		return "", fmt.Errorf("register filesystem image: %w", err)
	}
	return id, nil
}

// FilesystemRepository is where a host pushes a workspace's filesystem
// images.
func (i *Images) FilesystemRepository(workspace identity.WorkspaceID) string {
	return i.config.Registry + "/" + i.config.Repository + "/filesystems/" + workspace.String()
}

// FilesystemTarget is FilesystemRepository, whether the registry speaks
// plain HTTP, and its login.
func (i *Images) FilesystemTarget(ctx context.Context, workspace identity.WorkspaceID) (repository string, insecure bool, auth *Auth, err error) {
	if auth, err = i.login.auth(ctx); err != nil {
		return "", false, nil, err
	}
	return i.FilesystemRepository(workspace), i.config.Insecure, auth, nil
}
