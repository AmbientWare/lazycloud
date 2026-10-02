package compute_test

import (
	"testing"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

func TestHostLifecycleAllowsTheReservePathAndNothingThatSkipsAProof(t *testing.T) {
	allowed := [][2]compute.Phase{
		{compute.PhaseReady, compute.PhasePreparing},
		{compute.PhasePreparing, compute.PhaseStopping},
		{compute.PhasePreparing, compute.PhaseReady},
		{compute.PhasePreparing, compute.PhaseDraining},
		{compute.PhaseStopping, compute.PhaseStopped},
		{compute.PhaseStopping, compute.PhaseResuming},
		{compute.PhaseStopped, compute.PhaseResuming},
		{compute.PhaseStopped, compute.PhaseTerminating},
		{compute.PhaseResuming, compute.PhaseJoining},
		{compute.PhaseResuming, compute.PhaseTerminating},
		{compute.PhaseJoining, compute.PhasePreparing},
		{compute.PhaseStopped, compute.PhaseFailed},
	}
	for _, e := range allowed {
		if !e[0].CanBecome(e[1]) {
			t.Errorf("%s -> %s refused", e[0], e[1])
		}
	}
	// A stopped or resuming host serves only after its session joins it,
	// and only a proven host stops.
	refused := [][2]compute.Phase{
		{compute.PhaseStopped, compute.PhaseReady},
		{compute.PhaseResuming, compute.PhaseReady},
		{compute.PhaseReady, compute.PhaseStopping},
		{compute.PhaseReady, compute.PhaseStopped},
		{compute.PhaseStopping, compute.PhaseReady},
		{compute.PhaseDeleted, compute.PhaseFailed},
		{compute.PhaseTerminating, compute.PhaseReady},
	}
	for _, e := range refused {
		if e[0].CanBecome(e[1]) {
			t.Errorf("%s -> %s allowed", e[0], e[1])
		}
	}
}
