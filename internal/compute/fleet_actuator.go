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

	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// actionLease holds a claimed host while its pass makes EC2 calls; a
	// launch's move to a next pool renews it.
	actionLease = 2 * time.Minute
	// actuatorRecheck is how soon a pass that is done with a host looks at
	// it again. What keeps a call from repeating is the recorded stop,
	// refusal and terminate times and EC2's launch time, not the lease.
	actuatorRecheck = 10 * time.Second
	// providerCallsPerPass bounds the start, stop and terminate calls of
	// one pass, and launchesPerPass the hosts one launch pass walks.
	providerCallsPerPass = 10
	launchesPerPass      = 10
	// hibernateAfterStart is how soon after a start EC2 refuses to
	// hibernate: the guest has not set up its swap yet.
	hibernateAfterStart = 2 * time.Minute
	// providerDeadline is how long hibernation refusals are retried before
	// a plain stop, and how long a stop may stay pending before it is
	// forced. EC2 keeps a GPU instance stopping 5-7 minutes after its
	// guest powers off.
	providerDeadline = 10 * time.Minute
	// stopSettle is how long EC2 may still list a stopped instance as
	// running after it accepted the stop, and a terminated one after it
	// accepted the terminate.
	stopSettle = time.Minute
)

// ActuateResult counts one actuator pass's accepted EC2 calls.
type ActuateResult struct {
	Stopped    int
	Started    int
	Terminated int
}

// claimHosts claims up to batch cloud hosts in phases whose EC2 calls no
// other actuator holds. Every EC2 call on an instance a live host row
// records is made under such a claim, and every outcome write names the
// claim's token, so calls on one host never overlap across replicas and a
// holder whose lease passed writes nothing.
func (c *Compute) claimHosts(ctx context.Context, phases []string, batch int) ([]ClaimHostActionsRow, error) {
	claimed, err := c.queries.ClaimHostActions(ctx, ClaimHostActionsParams{
		Phases: phases, LeaseSeconds: actionLease.Seconds(), BatchSize: int32(batch), //nolint:gosec // A small constant.
		SettleSeconds: stopSettle.Seconds(), ReclaimGraceSeconds: ReclaimGrace.Seconds(),
	})
	if err != nil {
		return nil, fmt.Errorf("claim host actions: %w", err)
	}
	return claimed, nil
}

// Actuate makes the EC2 calls cloud hosts wait on: StopInstances for a
// platform host the agent proved ready to stop, StartInstances for a
// reserve asked to resume, and TerminateInstances for a terminating host
// or a drained one that runs nothing, cancelling a persistent Spot request
// first. The calls run outside any transaction against what EC2 reports
// now, so a retry after a lost answer repeats nothing EC2 already did, and
// a terminate is not asked again before EC2 can list it.
func (c *Compute) Actuate(ctx context.Context, logger *slog.Logger) (ActuateResult, error) {
	var result ActuateResult
	// Launch makes the EC2 calls of requested hosts.
	phases := []string{string(PhaseStopping), string(PhaseResuming), string(PhaseTerminating), string(PhaseDraining)}
	claimed, err := c.claimHosts(ctx, phases, providerCallsPerPass)
	if err != nil {
		return result, err
	}
	type owner struct {
		connection uuid.UUID
		region     string
	}
	groups := map[owner][]ClaimHostActionsRow{}
	for _, h := range claimed {
		key := owner{region: h.Region}
		if h.ConnectionID != nil {
			key.connection = *h.ConnectionID
		}
		groups[key] = append(groups[key], h)
	}
	for key, hosts := range groups {
		if err := c.actuateRegion(ctx, logger, key.region, hosts, &result); err != nil {
			if ctx.Err() != nil {
				return result, err
			}
			// The region's hosts wait a pass.
			logger.ErrorContext(ctx, "actuate hosts", "region", key.region, "error", err)
		}
	}
	release := ReleaseHostActionsParams{RecheckSeconds: actuatorRecheck.Seconds()}
	for _, h := range claimed {
		release.Ids, release.Tokens = append(release.Ids, h.ID), append(release.Tokens, h.ActionToken)
	}
	if len(release.Ids) == 0 {
		return result, nil
	}
	if err := c.queries.ReleaseHostActions(ctx, release); err != nil {
		return result, fmt.Errorf("release host actions: %w", err)
	}
	return result, nil
}

// actuateRegion makes the calls of one owner's claimed hosts in region.
func (c *Compute) actuateRegion(ctx context.Context, logger *slog.Logger, region string, hosts []ClaimHostActionsRow, result *ActuateResult) error {
	scope, err := c.scopeOf(ctx, hosts[0].ConnectionID)
	if err != nil {
		return err
	}
	client := c.aws().ec2(scope, region)
	ids := make([]string, len(hosts))
	for n, h := range hosts {
		ids[n] = deref(h.InstanceID)
	}
	instances, err := describeInstances(ctx, client, ids)
	if err != nil {
		return err
	}
	active, err := activeSpotRequests(ctx, client, hosts, instances)
	if err != nil {
		return err
	}
	for _, h := range hosts {
		instance, seen := instances[deref(h.InstanceID)]
		var err error
		switch Phase(h.Phase) {
		case PhaseStopping:
			if seen {
				err = c.stopReserve(ctx, logger, client, h, instance, active[deref(h.SpotRequestID)], result)
			}
		case PhaseResuming:
			if seen {
				err = c.startReserve(ctx, logger, client, h, instance, active[deref(h.SpotRequestID)], result)
			}
		case PhaseTerminating:
			err = c.terminateHost(ctx, logger, scope, h, instance, seen, result)
		case PhaseRequested, PhaseProvisioning, PhaseBooting, PhaseJoining, PhaseReady, PhaseDraining,
			PhasePreparing, PhaseStopped, PhaseDeleted, PhaseFailed:
			// The claim moves a draining host to terminating and selects none
			// of the others.
		}
		if err != nil {
			if ctx.Err() != nil {
				return err
			}
			logger.ErrorContext(ctx, "host action", "host_id", h.ID, "phase", h.Phase, "error", err)
		}
	}
	return nil
}

// terminateHost terminates a terminating host's instance unless EC2 lists
// it ending or gone, and records when, so no claim asks again before EC2
// can list it shutting down. Reconcile marks the host gone once EC2
// confirms it.
func (c *Compute) terminateHost(ctx context.Context, logger *slog.Logger, scope awsScope, h ClaimHostActionsRow, instance ec2types.Instance,
	seen bool, result *ActuateResult) error {
	if seen && instance.State.Name != ec2types.InstanceStateNameTerminated && instance.State.Name != ec2types.InstanceStateNameShuttingDown {
		if err := c.terminate(ctx, scope, h.Region, deref(h.InstanceID)); err != nil {
			return err
		}
		result.Terminated++
		logger.InfoContext(ctx, "host terminating", "host_id", h.ID, "instance_id", deref(h.InstanceID))
	}
	if _, err := c.queries.RecordTerminateRequested(ctx, RecordTerminateRequestedParams{ID: h.ID, Token: h.ActionToken}); err != nil {
		return fmt.Errorf("record terminate: %w", err)
	}
	return nil
}

// stopReserve moves a stopping host's instance toward stopped. It
// hibernates when the host sleeps that way and launched able to, no sooner
// than EC2 allows after a start; refusals retry until providerDeadline,
// then the host stops plainly, at once when EC2 says the instance cannot
// hibernate. A stop still pending past providerDeadline is forced.
func (c *Compute) stopReserve(ctx context.Context, logger *slog.Logger, client *ec2.Client, h ClaimHostActionsRow,
	instance ec2types.Instance, requestActive bool, result *ActuateResult) error {
	now := time.Now()
	hibernate := h.ReserveMode != nil && ReserveMode(*h.ReserveMode) == ReserveHibernate && h.HibernationConfigured &&
		instance.HibernationOptions != nil && aws.ToBool(instance.HibernationOptions.Configured)
	// A stop whose answer was lost, which EC2 took: what this stop saves is
	// not known yet, whatever the last one saved.
	lostEvidence := EvidenceUnavailable
	if hibernate {
		lostEvidence = EvidenceUnknown
	}
	if instance.InstanceLifecycle == ec2types.InstanceLifecycleTypeSpot && h.SpotRequestID == nil {
		// A one-time Spot instance cannot stop; it was bought to serve.
		return c.retireReserve(ctx, logger, h, "A one-time Spot instance cannot stop into the reserve", nil)
	}
	switch instance.State.Name {
	case ec2types.InstanceStateNameStopped:
		if h.StopRequestedAt == nil {
			if err := c.recordStop(ctx, h, lostEvidence, false); err != nil {
				return err
			}
		}
		return c.reserveStopped(ctx, h, stopReason(instance.StateReason), requestActive)
	case ec2types.InstanceStateNameStopping:
		if h.StopRequestedAt == nil {
			return c.recordStop(ctx, h, lostEvidence, false)
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
			return c.stopRefused(ctx, logger, h, fmt.Errorf("force stop: %w", err))
		}
		result.Stopped++
		return c.recordStop(ctx, h, EvidenceUnavailable, true)
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
			return c.recordStop(ctx, h, EvidenceUnknown, false)
		}
		code := awsCode(err)
		refusedSince := now
		if h.HibernateRefusedAt != nil {
			refusedSince = *h.HibernateRefusedAt
		}
		if code != "UnsupportedHibernationConfiguration" && now.Sub(refusedSince) < providerDeadline {
			logger.WarnContext(ctx, "EC2 refused to hibernate; retrying", "host_id", h.ID, "error", describeAWSError(err))
			if _, err := c.queries.RecordHibernateRefused(ctx, RecordHibernateRefusedParams{ID: h.ID, Token: h.ActionToken}); err != nil {
				return fmt.Errorf("record hibernate refusal: %w", err)
			}
			return nil
		}
		logger.WarnContext(ctx, "EC2 did not hibernate; stopping plainly", "host_id", h.ID, "error", describeAWSError(err))
	}
	if _, err := client.StopInstances(ctx, &ec2.StopInstancesInput{InstanceIds: []string{deref(h.InstanceID)}}); err != nil {
		return c.stopRefused(ctx, logger, h, fmt.Errorf("stop instance: %w", err))
	}
	logger.InfoContext(ctx, "reserve stopping", "host_id", h.ID, "instance_id", deref(h.InstanceID))
	result.Stopped++
	return c.recordStop(ctx, h, EvidenceUnavailable, false)
}

// stopRefused retries a stop EC2 may take later and retires a host whose
// stop it refused outright, so no host waits in stopping while it runs. The
// refusal cools the offer: a reserve bought there would be refused the same
// way.
func (c *Compute) stopRefused(ctx context.Context, logger *slog.Logger, h ClaimHostActionsRow, err error) error {
	if transientAWS(err) {
		return err
	}
	return c.retireReserve(ctx, logger, h, truncate("Stop refused: "+describeAWSError(err)), err)
}

// retireReserve sends a stopping host to terminating. A provider
// refusal, when given, cools the host's offer so the planner buys its
// replacement elsewhere.
func (c *Compute) retireReserve(ctx context.Context, logger *slog.Logger, h ClaimHostActionsRow, message string, refusal error) error {
	if err := transition(PhaseStopping, PhaseTerminating); err != nil {
		return err
	}
	logger.WarnContext(ctx, "retiring reserve", "host_id", h.ID, "reason", message)
	return inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		n, err := q.RetireReserve(ctx, RetireReserveParams{ID: h.ID, Token: h.ActionToken, Message: message})
		if err != nil {
			return fmt.Errorf("retire reserve: %w", err)
		}
		if n == 0 {
			return nil
		}
		if err := c.coolRefusal(ctx, q, h, refusal); err != nil {
			return err
		}
		return notifyMachines(ctx, tx, h.ID)
	})
}

// refuseStart returns a resuming host whose start EC2 refused for capacity
// to the reserve, which waits out the capacity cooldown before it resumes
// again. The refusal cools its zone's pool.
func (c *Compute) refuseStart(ctx context.Context, logger *slog.Logger, h ClaimHostActionsRow, refusal error) error {
	if err := transition(PhaseResuming, PhaseStopped); err != nil {
		return err
	}
	logger.WarnContext(ctx, "reserve start refused", "host_id", h.ID, "error", describeAWSError(refusal))
	return inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		n, err := q.ReturnStartToReserve(ctx, ReturnStartToReserveParams{
			ID: h.ID, Token: h.ActionToken, Message: truncate("Start refused: " + describeAWSError(refusal)),
		})
		if err != nil {
			return fmt.Errorf("return the reserve to stopped: %w", err)
		}
		if n == 0 {
			return nil
		}
		if err := c.coolRefusal(ctx, q, h, refusal); err != nil {
			return err
		}
		return notifyMachines(ctx, tx, h.ID)
	})
}

// coolRefusal cools the offer of a host EC2 refused, if it did: its zone
// for capacity, its region and quota class for quota, which also records
// the quota full.
func (c *Compute) coolRefusal(ctx context.Context, q *Queries, h ClaimHostActionsRow, refusal error) error {
	if refusal == nil || h.Market == nil {
		return nil
	}
	code := awsCode(refusal)
	zone := ""
	if capacityRefusal(code) && !quotaRefusal(code) {
		zone = h.AvailabilityZoneID
	}
	if err := q.InsertCooldown(ctx, InsertCooldownParams{
		ConnectionKey: string(KindPlatform), Region: h.Region, AvailabilityZoneID: zone, InstanceType: h.InstanceType,
		Market: *h.Market, Seconds: c.fleet.CapacityCooldown.Seconds(), MaxSeconds: (cooldownGrowth * c.fleet.CapacityCooldown).Seconds(),
		Reason: truncate(describeAWSError(refusal)),
	}); err != nil {
		return fmt.Errorf("insert cooldown: %w", err)
	}
	if quotaRefusal(code) {
		return c.refuseQuota(ctx, q, h.Region, h.InstanceType, Market(*h.Market))
	}
	return nil
}

func (c *Compute) recordStop(ctx context.Context, h ClaimHostActionsRow, evidence ImageEvidence, forced bool) error {
	if _, err := c.queries.RecordStopRequested(ctx, RecordStopRequestedParams{
		ID: h.ID, Token: h.ActionToken, ImageEvidence: string(evidence), Forced: forced,
	}); err != nil {
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

// reserveStopped records that a stopping host's instance stopped for
// reason and moves the host into the reserve. An unproven hibernation is
// saved when EC2 stopped it for the hibernation, and failed otherwise. EC2
// refuses to start a Spot instance until it disables the persistent
// request, 38-90 s after the stop; until then the host stays stopping with
// the stop recorded, so work may wait for it.
func (c *Compute) reserveStopped(ctx context.Context, h ClaimHostActionsRow, reason string, requestActive bool) error {
	evidence := string(EvidenceFailed)
	if reason == hibernatedReason {
		evidence = string(EvidenceSaved)
	}
	if requestActive {
		if err := c.queries.RecordInstanceStopped(ctx, RecordInstanceStoppedParams{ID: h.ID, Token: h.ActionToken, HibernationEvidence: evidence}); err != nil {
			return fmt.Errorf("record instance stopped: %w", err)
		}
		return nil
	}
	if err := transition(PhaseStopping, PhaseStopped); err != nil {
		return err
	}
	return inTx(ctx, c, func(tx pgx.Tx) error {
		n, err := c.queries.WithTx(tx).MarkReserveStopped(ctx, MarkReserveStoppedParams{ID: h.ID, Token: h.ActionToken, HibernationEvidence: evidence})
		if err != nil {
			return fmt.Errorf("mark reserve stopped: %w", err)
		}
		if n == 0 {
			return nil
		}
		return notifyMachines(ctx, tx, h.ID)
	})
}

// startReserve starts a resuming host's stopped instance once EC2 has
// disabled its Spot request, however long that takes; reconcile ends a
// reserve that never resumes. EC2 refusing the start for capacity returns
// the host to the reserve; the host's session moves it on once the agent
// says Hello.
func (c *Compute) startReserve(ctx context.Context, logger *slog.Logger, client *ec2.Client, h ClaimHostActionsRow,
	instance ec2types.Instance, requestActive bool, result *ActuateResult) error {
	switch instance.State.Name {
	case ec2types.InstanceStateNameStopped:
		if requestActive {
			return nil
		}
	case ec2types.InstanceStateNamePending, ec2types.InstanceStateNameRunning:
		// Started already, by a call whose answer was lost.
		return c.recordStart(ctx, h)
	case ec2types.InstanceStateNameStopping, ec2types.InstanceStateNameShuttingDown, ec2types.InstanceStateNameTerminated:
		// A stop in progress finishes first; a terminated instance is
		// reconcile's.
		return nil
	}
	spanCtx, span := c.hostSpan(ctx, h.ID, "compute.resume")
	_, err := client.StartInstances(spanCtx, &ec2.StartInstancesInput{InstanceIds: []string{deref(h.InstanceID)}})
	telemetry.Fail(span, err)
	if err == nil {
		logger.InfoContext(ctx, "reserve starting", "host_id", h.ID, "instance_id", deref(h.InstanceID))
		result.Started++
		return c.recordStart(ctx, h)
	}
	if !capacityRefusal(awsCode(err)) {
		return fmt.Errorf("start instance: %w", err)
	}
	return c.refuseStart(ctx, logger, h, err)
}

func (c *Compute) recordStart(ctx context.Context, h ClaimHostActionsRow) error {
	if _, err := c.queries.RecordStartRequested(ctx, RecordStartRequestedParams{ID: h.ID, Token: h.ActionToken}); err != nil {
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

// activeSpotRequests reports which persistent requests of hosts' stopped
// instances EC2 holds active.
func activeSpotRequests(ctx context.Context, client *ec2.Client, hosts []ClaimHostActionsRow,
	instances map[string]ec2types.Instance) (map[string]bool, error) {
	var requests []string
	for _, h := range hosts {
		if i, seen := instances[deref(h.InstanceID)]; seen && h.SpotRequestID != nil && Phase(h.Phase) != PhaseTerminating &&
			i.State.Name == ec2types.InstanceStateNameStopped {
			requests = append(requests, *h.SpotRequestID)
		}
	}
	out := map[string]bool{}
	if len(requests) == 0 {
		return out, nil
	}
	// A filter, unlike SpotInstanceRequestIds, leaves out an id EC2 no
	// longer lists instead of failing the call.
	pages := ec2.NewDescribeSpotInstanceRequestsPaginator(client, &ec2.DescribeSpotInstanceRequestsInput{
		Filters: []ec2types.Filter{
			{Name: aws.String("spot-instance-request-id"), Values: requests},
			{Name: aws.String("state"), Values: []string{string(ec2types.SpotInstanceStateActive)}},
		},
	})
	for pages.HasMorePages() {
		page, err := pages.NextPage(ctx)
		if err != nil {
			return nil, fmt.Errorf("describe spot requests: %w", err)
		}
		for _, r := range page.SpotInstanceRequests {
			out[aws.ToString(r.SpotInstanceRequestId)] = true
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

// terminate ends an instance in scope. A Spot reserve's persistent
// request is cancelled first, or EC2 would launch a replacement.
func (c *Compute) terminate(ctx context.Context, scope awsScope, region, instance string) error {
	client := c.aws().ec2(scope, region)
	request, err := c.spotRequestOf(ctx, instance)
	if err != nil {
		return err
	}
	if request != "" {
		return endSpotRequest(ctx, client, request)
	}
	_, err = client.TerminateInstances(ctx, &ec2.TerminateInstancesInput{InstanceIds: []string{instance}})
	if err != nil && awsCode(err) != "InvalidInstanceID.NotFound" {
		return fmt.Errorf("terminate %s: %w", instance, err)
	}
	return nil
}

// scopeOf is the credentials scope of the platform or a connection.
func (c *Compute) scopeOf(ctx context.Context, connection *uuid.UUID) (awsScope, error) {
	if connection == nil {
		return awsScope{key: string(KindPlatform)}, nil
	}
	row, err := c.queries.ConnectionScope(ctx, *connection)
	if errors.Is(err, pgx.ErrNoRows) {
		return awsScope{}, errors.New("the connection has no active authorization")
	}
	if err != nil {
		return awsScope{}, fmt.Errorf("read connection scope: %w", err)
	}
	return c.aws().assume(row.RoleArn, row.ExternalID, "lazycloud-fleet-"+connection.String()[:8], connection.String()), nil
}
