// Package compute owns hosts: enrollment, presence, capacity and loss. It
// supplies capacity to scheduling and never decides task outcomes.
package compute

import "github.com/google/uuid"

// HostID identifies an enrolled host.
type HostID uuid.UUID

func (id HostID) String() string { return uuid.UUID(id).String() }

// HostState is a host's availability for new containers.
type HostState string

const (
	HostOnline  HostState = "online"
	HostOffline HostState = "offline"
	HostLost    HostState = "lost"
	HostRetired HostState = "retired"
)
