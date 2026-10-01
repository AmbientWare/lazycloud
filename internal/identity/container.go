package identity

import (
	"fmt"

	"github.com/google/uuid"
)

// ContainerPrincipal is the caller of a container API request: code running
// in a workload container, which holds no credential. The host session
// derives it from the container's assignment to the calling host, so it
// speaks only for the workspace of the container's release.
type ContainerPrincipal struct {
	Container uuid.UUID
	Workspace Workspace
	// Task is set when the request names a task whose current attempt runs
	// on the container; Attempt and RootTask belong to it.
	Task     *uuid.UUID
	Attempt  *uuid.UUID
	RootTask *uuid.UUID
}

// AuthorizeWorkspace returns the container's workspace when name is it and
// it is not being deleted, as AuthorizeWorkspace does for users.
func (p ContainerPrincipal) AuthorizeWorkspace(name string) (Workspace, error) {
	if name != p.Workspace.Name {
		return Workspace{}, ErrForbidden
	}
	if p.Workspace.State == WorkspaceDeleting {
		return Workspace{}, &ConflictError{Message: fmt.Sprintf("workspace %s is being deleted", name)}
	}
	return p.Workspace, nil
}
