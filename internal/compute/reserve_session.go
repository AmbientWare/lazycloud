package compute

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// ReserveAttemptTimeout is how long a PrepareReserve waits for its answer
// before a fresh attempt supersedes it.
const ReserveAttemptTimeout = 2 * time.Minute

// ResumeOutcome is how a reserve came back from its last stop.
type ResumeOutcome string

const (
	// ResumeMemoryRestored: the same boot, after a sleep.
	ResumeMemoryRestored ResumeOutcome = "memory_restored"
	// ResumeColdBoot: a new boot.
	ResumeColdBoot ResumeOutcome = "cold_boot"
)

// ActivationKind is what a fleet activation sample measures.
type ActivationKind string

const (
	// ActivationProvision is a launch until the host's first session.
	ActivationProvision ActivationKind = "provision"
	// ActivationBoot is a requested start of a plainly stopped reserve
	// until its Hello.
	ActivationBoot ActivationKind = "boot"
	// ActivationResume is a requested start of a hibernated reserve until
	// its Hello.
	ActivationResume ActivationKind = "resume"
)

// activationReady is the outcome of a provision sample.
const activationReady = "ready"

// ReserveRequest is what a session asks of a host the planner is returning
// to the reserve.
type ReserveRequest struct {
	// ID names this return; a later one has another.
	ID   string
	Mode ReserveMode
	// GPUs the host must still find through its driver.
	GPUs int
}

// reserveRequestID names a return to the reserve by when the host entered
// preparing, which no heartbeat moves.
func reserveRequestID(phaseAt time.Time) string {
	return strconv.FormatInt(phaseAt.UnixMicro(), 10)
}

// ReserveStep is what an open session does for its host's reserve phase.
type ReserveStep string

const (
	// ReserveIdle: the host is in no reserve phase the session acts on.
	ReserveIdle ReserveStep = "idle"
	// ReservePrepare: ask the host to prove it may stop.
	ReservePrepare ReserveStep = "prepare"
	// ReserveWait: the host prepares while an agent update is offered or
	// in flight; asking now would only be refused.
	ReserveWait ReserveStep = "wait"
	// ReserveRejoin: the planner resumed a host that never stopped, so no
	// new Hello will come; the session ends and the agent's next Hello
	// settles the resume.
	ReserveRejoin ReserveStep = "rejoin"
)

// AgentState is what a session's Hello said about the agent's release.
type AgentState struct {
	Version   string
	Rejected  string
	Updatable bool
}

// ReserveSync returns what host's open session does for its reserve phase,
// and for ReservePrepare what it asks. A host prepared without a mode
// stops plainly.
func (c *Compute) ReserveSync(ctx context.Context, host HostID, agent AgentState) (ReserveStep, *ReserveRequest, error) {
	row, err := c.queries.ReserveSessionHost(ctx, uuid.UUID(host))
	if errors.Is(err, pgx.ErrNoRows) {
		return ReserveIdle, nil, nil
	}
	if err != nil {
		return "", nil, fmt.Errorf("read reserve host: %w", err)
	}
	switch Phase(row.Phase) {
	case PhaseResuming:
		return ReserveRejoin, nil, nil
	case PhasePreparing:
	default:
		return ReserveIdle, nil, nil
	}
	if row.Updating {
		return ReserveWait, nil, nil
	}
	update, err := c.UpdateFor(ctx, host, agent.Version, agent.Rejected, agent.Updatable)
	if err != nil {
		return "", nil, err
	}
	if update != nil {
		return ReserveWait, nil, nil
	}
	mode := ReserveStop
	if row.ReserveMode != nil {
		mode = ReserveMode(*row.ReserveMode)
	}
	return ReservePrepare, &ReserveRequest{ID: reserveRequestID(row.PhaseAt), Mode: mode, GPUs: int(row.GpuCount)}, nil
}

// ReserveAnswer is a host's answer to a ReserveRequest.
type ReserveAnswer struct {
	RequestID    string
	Attempt      uuid.UUID
	BootID       string
	AgentVersion string
	GPUs         int
	// Refused is why the agent cannot stop; empty when it is ready.
	Refused string
	// Updating marks a refusal for an agent update in flight.
	Updating bool
}

// ReserveVerdict is what an answer did to the host.
type ReserveVerdict string

const (
	// ReserveStopping: the host proved it may stop and the actuator stops
	// it.
	ReserveStopping ReserveVerdict = "stopping"
	// ReserveReturned: the host could not prove it and serves again.
	ReserveReturned ReserveVerdict = "returned"
	// ReserveStale: the answer is for a request or boot no longer current.
	ReserveStale ReserveVerdict = "stale"
	// ReserveDeferred: the agent is updating; the host keeps preparing and
	// is asked again after the update.
	ReserveDeferred ReserveVerdict = "deferred"
)

// AnswerReserve applies a host's answer for the current return to the
// reserve. The host stops only on the agent release it should run (the
// target, once the rollout reaches it), with every GPU
// it offers found through the driver and a passed preflight, and with no
// container still assigned; otherwise it returns to ready.
func (c *Compute) AnswerReserve(ctx context.Context, host HostID, answer ReserveAnswer) (ReserveVerdict, error) {
	if answer.Updating {
		return ReserveDeferred, nil
	}
	target, err := c.TargetRelease(ctx)
	if err != nil && !errors.Is(err, ErrNotFound) {
		return "", err
	}
	want := ""
	if target.Version != "" && rolloutBucket(host) < target.RolloutPercent {
		want = target.Version
	}
	verdict := ReserveStale
	err = pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, err := q.ReserveProofHost(ctx, uuid.UUID(host))
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrUnknownHost
		}
		if err != nil {
			return fmt.Errorf("read reserve host: %w", err)
		}
		if Phase(row.Phase) != PhasePreparing || reserveRequestID(row.PhaseAt) != answer.RequestID || row.BootID != answer.BootID {
			return nil
		}
		live, err := q.LiveHostContainers(ctx, &row.ID)
		if err != nil {
			return fmt.Errorf("count live containers: %w", err)
		}
		if reason := unproven(answer, want, row, live); reason != "" {
			if err := transition(PhasePreparing, PhaseReady); err != nil {
				return err
			}
			if _, err := q.SetHostPhase(ctx, SetHostPhaseParams{
				ID: row.ID, FromPhase: string(PhasePreparing), Phase: string(PhaseReady),
				Message: PhaseReady.Message() + "; not stopped: " + reason,
			}); err != nil {
				return fmt.Errorf("return host to ready: %w", err)
			}
			verdict = ReserveReturned
			return notifyMachines(ctx, tx, row.ID)
		}
		if err := transition(PhasePreparing, PhaseStopping); err != nil {
			return err
		}
		n, err := q.AcceptReserveStop(ctx, AcceptReserveStopParams{
			ID: row.ID, PhaseAt: row.PhaseAt, Message: PhaseStopping.Message(),
			SleepAttemptID: &answer.Attempt, SleepBootID: &answer.BootID,
			PreparedAgentVersion: &answer.AgentVersion, GpuProven: row.GpuCount > 0,
		})
		if err != nil {
			return fmt.Errorf("accept reserve stop: %w", err)
		}
		if n == 0 {
			return nil
		}
		verdict = ReserveStopping
		return notifyMachines(ctx, tx, row.ID)
	})
	if err != nil {
		return "", fmt.Errorf("answer reserve: %w", err)
	}
	return verdict, nil
}

// unproven says why an answer does not let the host stop, or is empty. want
// is the agent release the host should run, empty for any.
func unproven(answer ReserveAnswer, want string, row ReserveProofHostRow, live int64) string {
	var checks []PreflightCheck
	if err := json.Unmarshal(row.Preflight, &checks); err != nil {
		return "its preflight is unreadable"
	}
	switch {
	case answer.Refused != "":
		return answer.Refused
	case want != "" && answer.AgentVersion != want:
		return fmt.Sprintf("the agent runs %s, not the target release %s", answer.AgentVersion, want)
	case row.GpuCount > 0 && answer.GPUs < int(row.GpuCount):
		return fmt.Sprintf("the driver found %d of %d GPUs", answer.GPUs, row.GpuCount)
	case len(failedChecks(checks)) > 0:
		return "preflight failed: " + strings.Join(failedChecks(checks), "; ")
	case live > 0:
		return fmt.Sprintf("containers are still assigned to it (%d)", live)
	}
	return ""
}

// settleSession moves a host whose session is opening out of the reserve
// phases, in the session's transaction and before it records the new boot.
//
// The planner writes resuming together with resume_requested_at, so a
// resuming host is a requested resume. A stopping or stopped host that comes
// back with a sleep or a new boot was not asked to: it is kept out of
// placement and goes back through preparing, since the planner still holds
// it as a reserve. A host joining with a reserve mode, bought for the reserve
// or refreshing, prepares to stop instead of serving.
func settleSession(ctx context.Context, q *Queries, host uuid.UUID, open SessionOpen) error {
	h, err := q.ResumeHost(ctx, host)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read resuming host: %w", err)
	}
	phase := Phase(h.Phase)
	outcome := resumeOutcome(h, open)
	switch {
	case phase == PhaseJoining && h.SessionEpoch == 0 && HostKind(h.Kind) == KindPlatform && Provider(h.Provider) == ProviderAWS:
		if err := recordActivation(ctx, q, h, ActivationProvision, activationReady, h.CreatedAt); err != nil {
			return err
		}
	case phase == PhaseResuming || (outcome != nil && (phase == PhaseStopping || phase == PhaseStopped)):
		if outcome != nil {
			if err := q.RecordResumeOutcome(ctx, RecordResumeOutcomeParams{ID: host, Outcome: (*string)(outcome)}); err != nil {
				return fmt.Errorf("record resume outcome: %w", err)
			}
			if phase == PhaseResuming && h.ResumeRequestedAt != nil {
				kind := ActivationBoot
				if *outcome == ResumeMemoryRestored || ImageEvidence(h.ImageEvidence) == EvidenceSaved {
					kind = ActivationResume
				}
				if err := recordActivation(ctx, q, h, kind, string(*outcome), *h.ResumeRequestedAt); err != nil {
					return err
				}
			}
		}
		if phase != PhaseResuming {
			if err := movePhase(ctx, q, host, phase, PhaseResuming); err != nil {
				return err
			}
		}
		if err := movePhase(ctx, q, host, PhaseResuming, PhaseJoining); err != nil {
			return err
		}
		if err := q.EndLastStop(ctx, host); err != nil {
			return fmt.Errorf("end the last stop: %w", err)
		}
		phase = PhaseJoining
	}
	if phase == PhaseJoining && h.ReserveMode != nil {
		if err := movePhase(ctx, q, host, PhaseJoining, PhasePreparing); err != nil {
			return err
		}
	}
	return nil
}

// movePhase moves a host whose row the caller holds locked, so it cannot
// have left from.
func movePhase(ctx context.Context, q *Queries, host uuid.UUID, from, to Phase) error {
	moved, err := changePhase(ctx, q, host, from, to)
	if err != nil {
		return err
	}
	if !moved {
		return fmt.Errorf("host %s left %s while locked", host, from)
	}
	return nil
}

// resumeOutcome is how the host came back from the stop it last proved,
// when its Hello names that stop: a new boot, or a sleep in the same boot.
// Nil when the Hello names another attempt or the host has not slept.
func resumeOutcome(h ResumeHostRow, open SessionOpen) *ResumeOutcome {
	if h.SleepAttemptID == nil || open.SleepAttempt == nil || *h.SleepAttemptID != *open.SleepAttempt || h.SleepBootID == nil {
		return nil
	}
	switch {
	case open.BootID != *h.SleepBootID:
		return ptr(ResumeColdBoot)
	case open.SleptSeconds > 0:
		return ptr(ResumeMemoryRestored)
	}
	return nil
}

// recordActivation keeps one sample of how long an activation took since
// started, and drops samples older than a day.
func recordActivation(ctx context.Context, q *Queries, h ResumeHostRow, kind ActivationKind, outcome string, started time.Time) error {
	if err := q.InsertActivation(ctx, InsertActivationParams{
		Kind: string(kind), InstanceType: h.InstanceType, Region: h.Region, GpuType: h.GpuType, StartedAt: started, Outcome: outcome,
	}); err != nil {
		return fmt.Errorf("record activation: %w", err)
	}
	if err := q.PruneActivations(ctx); err != nil {
		return fmt.Errorf("prune activations: %w", err)
	}
	return nil
}
