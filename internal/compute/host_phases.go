package compute

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
)

// ErrPhaseTransition is a phase write the lifecycle does not allow.
var ErrPhaseTransition = errors.New("host phase transition not allowed")

// CanBecome reports whether a host in phase p may move to next. It is the
// host lifecycle:
//
//	requested -> provisioning -> booting -> joining -> ready
//	ready <-> draining -> terminating -> deleted
//	ready -> preparing -> stopping -> stopped -> resuming -> joining -> ready
//	joining -> preparing     a host bought for, or refreshing, the reserve
//	preparing -> ready       the agent refused to stop
//	preparing -> draining    a pass retires a reserve still preparing
//	stopping -> resuming     the agent says Hello before EC2 reports it stopped
//	stopping -> terminating  EC2 refused the stop
//	stopped -> terminating   a reserve retires
//	resuming -> terminating  EC2 refused the start for capacity
//	any -> failed
//
// Machines an account joins also go requested -> joining, failed ->
// requested on a new join, and any -> deleted when removed; a requested
// cloud host whose owner disconnects is deleted unlaunched.
func (p Phase) CanBecome(next Phase) bool {
	if next == PhaseFailed {
		return p != PhaseDeleted
	}
	if next == PhaseDeleted {
		return p != PhaseDeleted
	}
	switch p {
	case PhaseRequested:
		return next == PhaseProvisioning || next == PhaseJoining
	case PhaseProvisioning:
		return next == PhaseBooting || next == PhaseJoining
	case PhaseBooting:
		return next == PhaseJoining
	case PhaseJoining:
		return next == PhaseReady || next == PhasePreparing || next == PhaseDraining
	case PhaseReady:
		return next == PhaseDraining || next == PhasePreparing
	case PhaseDraining:
		return next == PhaseReady || next == PhaseTerminating
	case PhasePreparing:
		return next == PhaseStopping || next == PhaseReady || next == PhaseDraining
	case PhaseStopping:
		return next == PhaseStopped || next == PhaseResuming || next == PhaseTerminating
	case PhaseStopped:
		return next == PhaseResuming || next == PhaseTerminating
	case PhaseResuming:
		return next == PhaseJoining || next == PhaseTerminating
	case PhaseFailed:
		return next == PhaseRequested
	case PhaseTerminating, PhaseDeleted:
	}
	return false
}

// transition checks that from may become to; every phase write in Go goes
// through it before its guarded statement runs.
func transition(from, to Phase) error {
	if !from.CanBecome(to) {
		return fmt.Errorf("%w: %s to %s", ErrPhaseTransition, from, to)
	}
	return nil
}

// changePhase moves a host from one phase to the next with the phase's
// default message. It reports false when the host had already left from.
func changePhase(ctx context.Context, q *Queries, host uuid.UUID, from, to Phase) (bool, error) {
	if err := transition(from, to); err != nil {
		return false, err
	}
	n, err := q.SetHostPhase(ctx, SetHostPhaseParams{ID: host, FromPhase: string(from), Phase: string(to), Message: to.Message()})
	if err != nil {
		return false, fmt.Errorf("set host phase %s: %w", to, err)
	}
	return n == 1, nil
}
