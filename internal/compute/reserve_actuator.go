package compute

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

const (
	// actuatorLease holds a claimed host for one pass of provider calls.
	actuatorLease = 2 * time.Minute
	// providerCallsPerPass bounds the start, stop and terminate calls of
	// one pass.
	providerCallsPerPass = 10
	// hibernateAfterStart is how soon after a start EC2 refuses to
	// hibernate: the guest has not set up its swap yet.
	hibernateAfterStart = 2 * time.Minute
	// providerDeadline is how long hibernation refusals are retried before
	// a plain stop, and how long a stop may stay pending before it is
	// forced.
	providerDeadline = 10 * time.Minute
	// stopSettle is how long EC2 may still list a stopped instance as
	// running after it accepted the stop.
	stopSettle = time.Minute
)

// ActuateResult counts one actuator pass's accepted provider calls.
type ActuateResult struct {
	Stopped    int
	Started    int
	Terminated int
}

// Actuate performs the provider calls platform hosts wait on: StopInstances
// for a host the agent proved ready to stop, StartInstances for a reserve
// asked to resume, and termination, cancelling a Spot reserve's persistent
// request first. Each host is claimed with a lease; the calls run outside
// any transaction against what EC2 reports now, so a retry after a lost
// answer repeats nothing EC2 already did. Outcomes are written with a phase
// guard.
func (c *Compute) Actuate(ctx context.Context, logger *slog.Logger) (ActuateResult, error) {
	var result ActuateResult
	claimed, err := c.queries.ClaimProviderActions(ctx, ClaimProviderActionsParams{
		LeaseSeconds: actuatorLease.Seconds(), BatchSize: providerCallsPerPass,
	})
	if err != nil {
		return result, fmt.Errorf("claim provider actions: %w", err)
	}
	byRegion := map[string][]ClaimProviderActionsRow{}
	for _, h := range claimed {
		byRegion[h.Region] = append(byRegion[h.Region], h)
	}
	scope := awsScope{key: string(KindPlatform)}
	for region, hosts := range byRegion {
		client := c.aws().ec2(scope, region)
		ids := make([]string, len(hosts))
		for n, h := range hosts {
			ids[n] = deref(h.InstanceID)
		}
		instances, err := describeInstances(ctx, client, ids)
		if err != nil {
			if ctx.Err() != nil {
				return result, err
			}
			logger.ErrorContext(ctx, "describe reserve instances", "region", region, "error", err)
			continue
		}
		for _, h := range hosts {
			instance, seen := instances[deref(h.InstanceID)]
			var err error
			switch Phase(h.Phase) {
			case PhaseStopping:
				if seen {
					err = c.stopReserve(ctx, logger, client, h, instance, &result)
				}
			case PhaseResuming:
				if seen {
					err = c.startReserve(ctx, logger, client, h, instance, &result)
				}
			case PhaseTerminating:
				// A vanished instance is reconcile's to mark deleted.
				if seen && instance.State.Name != ec2types.InstanceStateNameTerminated {
					if err = c.terminate(ctx, scope, region, deref(h.InstanceID)); err == nil {
						result.Terminated++
					}
				}
			case PhaseRequested, PhaseProvisioning, PhaseBooting, PhaseJoining, PhaseReady, PhaseDraining,
				PhasePreparing, PhaseStopped, PhaseDeleted, PhaseFailed:
				// The claim selects none of these.
			}
			if err != nil {
				if ctx.Err() != nil {
					return result, err
				}
				logger.ErrorContext(ctx, "reserve action", "host_id", h.ID, "phase", h.Phase, "error", err)
			}
		}
	}
	return result, nil
}

// stopReserve moves a stopping host's instance toward stopped. It
// hibernates when the host sleeps that way and launched able to, no sooner
// than EC2 allows after a start; refusals retry until providerDeadline,
// then the host stops plainly, at once when EC2 says the instance cannot
// hibernate. A stop still pending past providerDeadline is forced.
func (c *Compute) stopReserve(ctx context.Context, logger *slog.Logger, client *ec2.Client, h ClaimProviderActionsRow,
	instance ec2types.Instance, result *ActuateResult) error {
	now := time.Now()
	hibernate := h.ReserveMode != nil && ReserveMode(*h.ReserveMode) == ReserveHibernate && h.HibernationConfigured &&
		instance.HibernationOptions != nil && aws.ToBool(instance.HibernationOptions.Configured)
	switch instance.State.Name {
	case ec2types.InstanceStateNameStopped:
		return c.reserveStopped(ctx, h.ID, stopReason(instance.StateReason))
	case ec2types.InstanceStateNameStopping:
		if h.StopRequestedAt == nil {
			// The stop's answer was lost; EC2 took it.
			evidence := EvidenceUnavailable
			if hibernate {
				evidence = EvidenceUnknown
			}
			return c.recordStop(ctx, h.ID, evidence, false)
		}
		requested := *h.StopRequestedAt
		if h.ForceStopAt != nil {
			requested = *h.ForceStopAt
		}
		if now.Sub(requested) <= providerDeadline {
			return nil
		}
		logger.WarnContext(ctx, "reserve still stopping; forcing the stop", "host_id", h.ID, "instance_id", deref(h.InstanceID))
		if _, err := client.StopInstances(ctx, &ec2.StopInstancesInput{InstanceIds: []string{deref(h.InstanceID)}, Force: aws.Bool(true)}); err != nil {
			return fmt.Errorf("force stop: %w", err)
		}
		result.Stopped++
		return c.recordStop(ctx, h.ID, EvidenceUnavailable, true)
	case ec2types.InstanceStateNameRunning:
	case ec2types.InstanceStateNamePending, ec2types.InstanceStateNameShuttingDown, ec2types.InstanceStateNameTerminated:
		// Pending settles first; a terminated instance is reconcile's.
		return nil
	}
	if h.StopRequestedAt != nil && now.Sub(*h.StopRequestedAt) < stopSettle {
		return nil
	}
	if hibernate {
		if instance.LaunchTime != nil && now.Sub(*instance.LaunchTime) < hibernateAfterStart {
			return nil
		}
		_, err := client.StopInstances(ctx, &ec2.StopInstancesInput{InstanceIds: []string{deref(h.InstanceID)}, Hibernate: aws.Bool(true)})
		if err == nil {
			logger.InfoContext(ctx, "reserve hibernating", "host_id", h.ID, "instance_id", deref(h.InstanceID))
			result.Stopped++
			return c.recordStop(ctx, h.ID, EvidenceUnknown, false)
		}
		code := awsCode(err)
		refusedSince := now
		if h.HibernateRefusedAt != nil {
			refusedSince = *h.HibernateRefusedAt
		}
		if code != "UnsupportedHibernationConfiguration" && now.Sub(refusedSince) < providerDeadline {
			logger.WarnContext(ctx, "EC2 refused to hibernate; retrying", "host_id", h.ID, "error", describeAWSError(err))
			if _, err := c.queries.RecordHibernateRefused(ctx, h.ID); err != nil {
				return fmt.Errorf("record hibernate refusal: %w", err)
			}
			return nil
		}
		logger.WarnContext(ctx, "EC2 did not hibernate; stopping plainly", "host_id", h.ID, "error", describeAWSError(err))
	}
	if _, err := client.StopInstances(ctx, &ec2.StopInstancesInput{InstanceIds: []string{deref(h.InstanceID)}}); err != nil {
		return fmt.Errorf("stop instance: %w", err)
	}
	logger.InfoContext(ctx, "reserve stopping", "host_id", h.ID, "instance_id", deref(h.InstanceID))
	result.Stopped++
	return c.recordStop(ctx, h.ID, EvidenceUnavailable, false)
}

func (c *Compute) recordStop(ctx context.Context, host uuid.UUID, evidence ImageEvidence, forced bool) error {
	if _, err := c.queries.RecordStopRequested(ctx, RecordStopRequestedParams{ID: host, ImageEvidence: string(evidence), Forced: forced}); err != nil {
		return fmt.Errorf("record stop: %w", err)
	}
	return nil
}

// hibernatedReason is EC2's stop reason for an instance it stopped by
// hibernating. EC2 gives it whether or not the guest saved its image, so it
// claims the image; the agent's resume report proves it.
const hibernatedReason = "Client.UserInitiatedHibernate"

func stopReason(r *ec2types.StateReason) string {
	if r == nil {
		return ""
	}
	return aws.ToString(r.Code)
}

// reserveStopped moves a stopping host whose instance EC2 reports stopped
// for reason into the reserve. An unproven hibernation is saved when EC2
// stopped it for the hibernation, and failed otherwise.
func (c *Compute) reserveStopped(ctx context.Context, host uuid.UUID, reason string) error {
	if err := transition(PhaseStopping, PhaseStopped); err != nil {
		return err
	}
	evidence := EvidenceFailed
	if reason == hibernatedReason {
		evidence = EvidenceSaved
	}
	return inTx(ctx, c, func(tx pgx.Tx) error {
		n, err := c.queries.WithTx(tx).MarkReserveStopped(ctx, MarkReserveStoppedParams{ID: host, HibernationEvidence: string(evidence)})
		if err != nil {
			return fmt.Errorf("mark reserve stopped: %w", err)
		}
		if n == 0 {
			return nil
		}
		return notifyMachines(ctx, tx, host)
	})
}

// startReserve starts a resuming host's stopped instance. EC2 refusing it
// for capacity retires the host and cools its offer, so the planner buys
// elsewhere; the host's session moves it on once the agent says Hello.
func (c *Compute) startReserve(ctx context.Context, logger *slog.Logger, client *ec2.Client, h ClaimProviderActionsRow,
	instance ec2types.Instance, result *ActuateResult) error {
	switch instance.State.Name {
	case ec2types.InstanceStateNameStopped:
	case ec2types.InstanceStateNamePending, ec2types.InstanceStateNameRunning:
		// Started already: a lost answer, or a resume of a host whose
		// stop had not begun.
		return c.recordStart(ctx, h.ID)
	case ec2types.InstanceStateNameStopping, ec2types.InstanceStateNameShuttingDown, ec2types.InstanceStateNameTerminated:
		// A stop in progress finishes first; a terminated instance is
		// reconcile's.
		return nil
	}
	_, err := client.StartInstances(ctx, &ec2.StartInstancesInput{InstanceIds: []string{deref(h.InstanceID)}})
	if err == nil {
		logger.InfoContext(ctx, "reserve starting", "host_id", h.ID, "instance_id", deref(h.InstanceID))
		result.Started++
		return c.recordStart(ctx, h.ID)
	}
	if !capacityRefusal(awsCode(err)) {
		return fmt.Errorf("start instance: %w", err)
	}
	refusal := describeAWSError(err)
	logger.WarnContext(ctx, "EC2 refused to start a reserve; retiring it", "host_id", h.ID, "error", refusal)
	if err := transition(PhaseResuming, PhaseTerminating); err != nil {
		return err
	}
	return inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		n, err := q.RefuseResume(ctx, RefuseResumeParams{ID: h.ID, Message: truncate("Start refused: " + refusal)})
		if err != nil {
			return fmt.Errorf("refuse resume: %w", err)
		}
		if n == 0 {
			return nil
		}
		if h.Market != nil {
			if err := q.InsertCooldown(ctx, InsertCooldownParams{
				ConnectionKey: string(KindPlatform), Region: h.Region, InstanceType: h.InstanceType, Market: *h.Market,
				Seconds: c.fleet.CapacityCooldown.Seconds(), Reason: truncate(refusal),
			}); err != nil {
				return fmt.Errorf("insert cooldown: %w", err)
			}
		}
		return notifyMachines(ctx, tx, h.ID)
	})
}

func (c *Compute) recordStart(ctx context.Context, host uuid.UUID) error {
	if _, err := c.queries.RecordStartRequested(ctx, host); err != nil {
		return fmt.Errorf("record start: %w", err)
	}
	return nil
}

// describeInstances reads instances by id. A filter, unlike InstanceIds,
// leaves out an id EC2 no longer lists instead of failing the call.
func describeInstances(ctx context.Context, client *ec2.Client, ids []string) (map[string]ec2types.Instance, error) {
	out := map[string]ec2types.Instance{}
	pages := ec2.NewDescribeInstancesPaginator(client, &ec2.DescribeInstancesInput{
		Filters: []ec2types.Filter{{Name: aws.String("instance-id"), Values: ids}},
	})
	for pages.HasMorePages() {
		page, err := pages.NextPage(ctx)
		if err != nil {
			return nil, fmt.Errorf("describe instances: %w", err)
		}
		for _, r := range page.Reservations {
			for _, i := range r.Instances {
				if i.State != nil {
					out[aws.ToString(i.InstanceId)] = i
				}
			}
		}
	}
	return out, nil
}

// endSpotRequest cancels a persistent Spot request and terminates every
// instance it launched. The request goes first: EC2 relaunches a live
// request's instance when it ends. Instances are found by the request id,
// so a relaunched instance the fleet never recorded ends too.
func endSpotRequest(ctx context.Context, client *ec2.Client, request string) error {
	described, err := client.DescribeSpotInstanceRequests(ctx, &ec2.DescribeSpotInstanceRequestsInput{
		SpotInstanceRequestIds: []string{request},
	})
	switch {
	case awsCode(err) == "InvalidSpotInstanceRequestID.NotFound":
	case err != nil:
		return fmt.Errorf("describe spot request: %w", err)
	case len(described.SpotInstanceRequests) != 1 || aws.ToString(described.SpotInstanceRequests[0].SpotInstanceRequestId) != request:
		return fmt.Errorf("spot request %s: EC2 described another request", request)
	default:
		switch described.SpotInstanceRequests[0].State {
		case ec2types.SpotInstanceStateCancelled, ec2types.SpotInstanceStateClosed, ec2types.SpotInstanceStateFailed:
		case ec2types.SpotInstanceStateOpen, ec2types.SpotInstanceStateActive, ec2types.SpotInstanceStateDisabled:
			if _, err := client.CancelSpotInstanceRequests(ctx, &ec2.CancelSpotInstanceRequestsInput{
				SpotInstanceRequestIds: []string{request},
			}); err != nil && awsCode(err) != "InvalidSpotInstanceRequestID.NotFound" {
				return fmt.Errorf("cancel spot request %s: %w", request, err)
			}
		}
	}
	pages := ec2.NewDescribeInstancesPaginator(client, &ec2.DescribeInstancesInput{
		Filters: []ec2types.Filter{{Name: aws.String("spot-instance-request-id"), Values: []string{request}}},
	})
	var live []string
	for pages.HasMorePages() {
		page, err := pages.NextPage(ctx)
		if err != nil {
			return fmt.Errorf("describe spot request instances: %w", err)
		}
		for _, r := range page.Reservations {
			for _, i := range r.Instances {
				if aws.ToString(i.SpotInstanceRequestId) != request {
					return fmt.Errorf("spot request %s: EC2 listed an instance of another request", request)
				}
				if i.State != nil && i.State.Name != ec2types.InstanceStateNameTerminated &&
					i.State.Name != ec2types.InstanceStateNameShuttingDown {
					live = append(live, aws.ToString(i.InstanceId))
				}
			}
		}
	}
	if len(live) == 0 {
		return nil
	}
	if _, err := client.TerminateInstances(ctx, &ec2.TerminateInstancesInput{InstanceIds: live}); err != nil &&
		awsCode(err) != "InvalidInstanceID.NotFound" {
		return fmt.Errorf("terminate spot request instances: %w", err)
	}
	return nil
}

// spotRequestOf is the persistent Spot request that launched instance, or
// "" when none or no host records it.
func (c *Compute) spotRequestOf(ctx context.Context, instance string) (string, error) {
	request, err := c.queries.SpotRequestOfInstance(ctx, &instance)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("read spot request: %w", err)
	}
	return request, nil
}
