// Package compute owns hosts: enrollment, presence, capacity and loss, joined
// machines, AWS account connections, the cloud fleet that provisions and
// retires instances, interruptions and agent releases. It supplies capacity
// to scheduling and never decides task outcomes.
package compute

import (
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database"
)

// ChannelCompute wakes the fleet loops; the payload is a host id.
const ChannelCompute database.Channel = "lc_compute"

// HostID identifies an enrolled host.
type HostID uuid.UUID

func (id HostID) String() string { return uuid.UUID(id).String() }

// MarshalText writes the id as a UUID string, so JSON logs and documents
// carry it in the form every other id takes.
func (id HostID) MarshalText() ([]byte, error) { return uuid.UUID(id).MarshalText() } //nolint:wrapcheck // uuid encodes it

// HostState is a host's session authority: whether it may act and whether
// its containers count as running.
type HostState string

const (
	HostOnline  HostState = "online"
	HostOffline HostState = "offline"
	HostLost    HostState = "lost"
	HostRetired HostState = "retired"
)

// HostKind says whose capacity a host is.
type HostKind string

const (
	// KindPlatform is LazyCloud's capacity, for workspaces on platform
	// compute.
	KindPlatform HostKind = "platform"
	// KindConnection runs in a customer's connected AWS account.
	KindConnection HostKind = "connection"
	// KindMachine is a machine an account joined; it runs only workloads
	// pinned to it from the workspaces it serves.
	KindMachine HostKind = "machine"
)

// Provider is who runs the host.
type Provider string

const (
	ProviderAgent Provider = "agent"
	ProviderAWS   Provider = "aws"
)

// Phase is a host's lifecycle as users see it.
type Phase string

const (
	PhaseRequested    Phase = "requested"
	PhaseProvisioning Phase = "provisioning"
	PhaseBooting      Phase = "booting"
	PhaseJoining      Phase = "joining"
	PhaseReady        Phase = "ready"
	PhaseDraining     Phase = "draining"
	PhasePreparing    Phase = "preparing"
	PhaseStopping     Phase = "stopping"
	PhaseStopped      Phase = "stopped"
	PhaseResuming     Phase = "resuming"
	PhaseTerminating  Phase = "terminating"
	PhaseDeleted      Phase = "deleted"
	PhaseFailed       Phase = "failed"
)

// Message is the phase's default lifecycle message.
func (p Phase) Message() string {
	switch p {
	case PhaseRequested:
		return "Waiting for the machine to be launched"
	case PhaseProvisioning:
		return "Instance is starting; waiting for the node to report"
	case PhaseBooting:
		return "Node is installing the agent"
	case PhaseJoining:
		return "Agent joined; waiting for its first heartbeat"
	case PhaseReady:
		return "Ready for workloads"
	case PhaseDraining:
		return "Draining; no new work is placed here"
	case PhasePreparing:
		return "Preparing to stop into the reserve"
	case PhaseStopping:
		return "Stopping into the reserve"
	case PhaseStopped:
		return "Stopped in the reserve"
	case PhaseResuming:
		return "Starting from the reserve"
	case PhaseTerminating:
		return "Shutting down"
	case PhaseDeleted:
		return "Removed"
	case PhaseFailed:
		return "Failed"
	}
	return string(p)
}

// ReserveMode is how a platform host sleeps in the reserve.
type ReserveMode string

const (
	// ReserveStop stops the instance; it boots again on start.
	ReserveStop ReserveMode = "stop"
	// ReserveHibernate saves memory to the root volume, so a start
	// restores the running agent.
	ReserveHibernate ReserveMode = "hibernate"
)

// ImageEvidence is whether a reserve's last stop saved a hibernation image.
type ImageEvidence string

const (
	// EvidenceUnknown is a hibernation still stopping.
	EvidenceUnknown ImageEvidence = "unknown"
	// EvidenceSaved is a hibernation EC2 stopped the instance for; the
	// agent's resume report proves the image.
	EvidenceSaved ImageEvidence = "saved"
	// EvidenceFailed is a hibernation EC2 stopped the instance for some
	// other reason; the host boots cold on start.
	EvidenceFailed ImageEvidence = "failed"
	// EvidenceUnavailable is a plain or forced stop, which saves nothing.
	EvidenceUnavailable ImageEvidence = "unavailable"
)

// Failure says why a host failed.
type Failure string

const (
	FailureProviderIdentity  Failure = "provider_identity_failed"
	FailureEnrollment        Failure = "agent_enrollment_failed"
	FailureBootstrapTimedOut Failure = "bootstrap_timed_out"
	FailurePreflight         Failure = "host_preflight_failed"
	FailureServiceLost       Failure = "service_lost"
	FailureProviderStopped   Failure = "provider_stopped"
	FailureProviderGone      Failure = "provider_terminated"
	FailureUnknown           Failure = "unknown"
)

// CapacityState says whether a ready host takes new work.
type CapacityState string

const (
	CapacityAvailable  CapacityState = "available"
	CapacityDraining   CapacityState = "draining"
	CapacityPreempting CapacityState = "preempting"
)

// Market is how a cloud instance is bought.
type Market string

const (
	MarketSpot     Market = "spot"
	MarketOnDemand Market = "on_demand"
)
