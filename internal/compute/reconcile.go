package compute

import (
	"context"
	"encoding/json"
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
	// serviceLostAfter is how long a ready cloud host may stay lost before
	// its instance is replaced.
	serviceLostAfter = 5 * time.Minute
	// launchGrace is how long EC2 may take to list a launched instance.
	launchGrace = 2 * time.Minute
)

// Reconcile compares cloud hosts with what EC2 reports for the fleet tag in
// each owner's regions and writes phases only: a host whose instance is
// gone ends, deleted when the fleet asked or failed otherwise, and its
// containers stop through execution; a host whose instance must end, as
// one stopped unasked, one that never enrolled or one lost for long,
// terminates with its failure recorded, and the actuator ends the instance.
// Reconcile ends only instances and Spot requests no live host records.
func (c *Compute) Reconcile(ctx context.Context, logger *slog.Logger) error {
	pairs, err := c.reconcileRegions(ctx)
	if err != nil {
		return err
	}
	for _, p := range pairs {
		if err := c.reconcileRegion(ctx, logger, p.connection, p.region); err != nil {
			if ctx.Err() != nil {
				return err
			}
			logger.ErrorContext(ctx, "reconcile region", "connection_id", p.connection, "region", p.region, "error", err)
		}
	}
	return nil
}

type ownerRegion struct {
	connection *uuid.UUID
	region     string
}

// reconcileRegions are every region the fleet may hold instances in: the
// platform's networks, each managed connection's networks, and any region a
// cloud host still records, so an orphan is found where no host lives.
func (c *Compute) reconcileRegions(ctx context.Context) ([]ownerRegion, error) {
	seen := map[string]bool{}
	var out []ownerRegion
	add := func(connection *uuid.UUID, region string) {
		key := region
		if connection != nil {
			key = connection.String() + "/" + region
		}
		if region != "" && !seen[key] {
			seen[key] = true
			out = append(out, ownerRegion{connection: connection, region: region})
		}
	}
	for region := range c.fleet.Networks {
		add(nil, region)
	}
	connections, err := c.queries.ManagedConnectionNetworks(ctx)
	if err != nil {
		return nil, fmt.Errorf("list connection networks: %w", err)
	}
	for _, conn := range connections {
		var networks map[string]Network
		if err := json.Unmarshal(conn.Networks, &networks); err != nil {
			return nil, fmt.Errorf("decode networks of connection %s: %w", conn.ID, err)
		}
		for region := range networks {
			add(&conn.ID, region)
		}
	}
	pairs, err := c.queries.FleetRegions(ctx)
	if err != nil {
		return nil, fmt.Errorf("list fleet regions: %w", err)
	}
	for _, p := range pairs {
		add(p.ConnectionID, p.Region)
	}
	return out, nil
}

type observedInstance struct {
	state ec2types.InstanceStateName
	host  string
	// started is when EC2 last started the instance.
	started time.Time
}

func (c *Compute) reconcileRegion(ctx context.Context, logger *slog.Logger, connection *uuid.UUID, region string) error {
	scope, err := c.scopeOf(ctx, connection)
	if err != nil {
		return err
	}
	// Hosts first: an instance launched after this read is not in it, so
	// it cannot look like an orphan, and one launched just before may not
	// be listed by EC2 yet, which launchGrace covers.
	hosts, err := c.queries.FleetHostsInRegion(ctx, FleetHostsInRegionParams{Region: region, ConnectionID: connection})
	if err != nil {
		return fmt.Errorf("list region hosts: %w", err)
	}
	client := c.aws().ec2(scope, region)
	observed := map[string]observedInstance{}
	pages := ec2.NewDescribeInstancesPaginator(client, &ec2.DescribeInstancesInput{
		Filters: []ec2types.Filter{{Name: aws.String("tag:" + tagFleet), Values: []string{c.fleet.Name}}},
	})
	for pages.HasMorePages() {
		page, err := pages.NextPage(ctx)
		if err != nil {
			return fmt.Errorf("describe instances: %w", err)
		}
		for _, r := range page.Reservations {
			for _, i := range r.Instances {
				o := observedInstance{state: i.State.Name, started: aws.ToTime(i.LaunchTime)}
				for _, t := range i.Tags {
					if aws.ToString(t.Key) == tagHost {
						o.host = aws.ToString(t.Value)
					}
				}
				observed[aws.ToString(i.InstanceId)] = o
			}
		}
	}
	wanted := map[string]bool{}
	for _, h := range hosts {
		if h.InstanceID == nil {
			continue
		}
		instance := *h.InstanceID
		wanted[instance] = true
		o, seen := observed[instance]
		if !seen && Phase(h.Phase) != PhaseTerminating && h.LaunchedAt != nil && time.Since(*h.LaunchedAt) < launchGrace {
			// EC2 lists new instances eventually.
			continue
		}
		gone := !seen || o.state == ec2types.InstanceStateNameTerminated || o.state == ec2types.InstanceStateNameShuttingDown
		stopped := seen && (o.state == ec2types.InstanceStateNameStopped || o.state == ec2types.InstanceStateNameStopping)
		updating := h.UpdatingUntil != nil && time.Now().Before(*h.UpdatingUntil)
		phase := Phase(h.Phase)
		// A reserve's instance stops because the fleet asked: stopping and
		// stopped are expected, and the actuator completes the stop.
		reserve := phase == PhaseStopping || phase == PhaseStopped || phase == PhaseResuming
		// Every write is from the phase read above: a host the session or
		// actuator moved since keeps its new phase.
		end := func(failure Failure, message string) error {
			return c.endHost(ctx, h.ID, phase, failure, message)
		}
		switch {
		case phase == PhaseTerminating && gone:
			// The termination the fleet asked for, with the failure that
			// asked for it when one did.
			err = c.hostGone(ctx, h.ID, phase, Failure(deref(h.Failure)), h.PhaseMessage)
		case phase == PhaseTerminating:
			// The actuator terminates it.
		case gone && h.InterruptionAt != nil:
			// The reclaim its notice announced.
			err = c.hostGone(ctx, h.ID, phase, "", "")
		case gone:
			err = c.hostGone(ctx, h.ID, phase, FailureProviderGone, "The provider terminated the instance")
		case phase == PhaseStopping && time.Since(h.PhaseAt) > c.fleet.BootTimeout+providerDeadline:
			err = end(FailureUnknown, "The reserve did not stop in time")
		case phase == PhaseResuming && time.Since(h.PhaseAt) > c.fleet.BootTimeout+providerDeadline:
			err = end(FailureBootstrapTimedOut, "The reserve did not resume in time")
		case phase == PhaseStopped && !stopped && time.Since(o.started) > c.fleet.BootTimeout:
			// Started without a resume and never put back.
			err = end(FailureUnknown, "The reserve started without a resume")
		case reserve:
		case stopped:
			err = end(FailureProviderStopped, "The provider stopped the instance")
		case (phase == PhaseProvisioning || phase == PhaseBooting) && h.LaunchedAt != nil &&
			time.Since(*h.LaunchedAt) > c.fleet.BootTimeout:
			err = end(FailureBootstrapTimedOut, "The instance did not enroll in time")
		case phase == PhaseJoining && time.Since(h.PhaseAt) > c.fleet.BootTimeout:
			err = end(FailureEnrollment, "The agent enrolled but never opened a session")
		case phase == PhaseProvisioning && o.state == ec2types.InstanceStateNameRunning:
			_, err = changePhase(ctx, c.queries, h.ID, PhaseProvisioning, PhaseBooting)
		case phase == PhaseReady && HostState(h.State) == HostLost && !updating && h.LastSeenAt != nil &&
			time.Since(*h.LastSeenAt) > serviceLostAfter:
			err = end(FailureServiceLost, "The agent stopped reporting")
		}
		if err != nil {
			logger.ErrorContext(ctx, "reconcile host", "host_id", h.ID, "instance_id", instance, "error", err)
		}
	}
	if connection == nil {
		// Reserves are platform hosts; only the platform holds persistent
		// Spot requests.
		if err := c.cancelOrphanSpotRequests(ctx, logger, client); err != nil {
			logger.ErrorContext(ctx, "cancel orphan spot requests", "region", region, "error", err)
		}
	}
	var orphans []string
	var tagged []uuid.UUID
	for instance, o := range observed {
		if wanted[instance] || o.state == ec2types.InstanceStateNameTerminated || o.state == ec2types.InstanceStateNameShuttingDown {
			continue
		}
		if id, err := uuid.Parse(o.host); err == nil {
			tagged = append(tagged, id)
		}
		orphans = append(orphans, instance)
	}
	if len(orphans) == 0 {
		return nil
	}
	// A live host keeps the instance it records, read after EC2 listed it
	// so a launch recorded meanwhile keeps its own, or any while it records
	// none, as when its launch's answer was lost. One a stale claim made
	// for a host that has recorded another ends.
	live, err := c.queries.LiveHosts(ctx, tagged)
	if err != nil {
		return fmt.Errorf("read live hosts: %w", err)
	}
	keeps := map[string]*string{}
	for _, h := range live {
		keeps[h.ID.String()] = h.InstanceID
	}
	for _, instance := range orphans {
		if recorded, alive := keeps[observed[instance].host]; alive && (recorded == nil || *recorded == instance) {
			continue
		}
		if err := c.terminate(ctx, scope, region, instance); err != nil {
			logger.ErrorContext(ctx, "terminate orphan", "instance_id", instance, "error", err)
			continue
		}
		logger.WarnContext(ctx, "orphan instance terminated", "instance_id", instance, "region", region)
	}
	return nil
}

// endHost moves a cloud host in phase from whose instance must end to
// terminating with its failure recorded; a host that left from since it
// was read is left alone. Its containers stop through execution in the
// same transaction.
func (c *Compute) endHost(ctx context.Context, host uuid.UUID, from Phase, failure Failure, message string) error {
	if err := transition(from, PhaseTerminating); err != nil {
		return err
	}
	return inTx(ctx, c, func(tx pgx.Tx) error {
		n, err := c.queries.WithTx(tx).EndHost(ctx, EndHostParams{ID: host, FromPhase: string(from), Failure: ptr(string(failure)), Message: message})
		if err != nil {
			return fmt.Errorf("end host: %w", err)
		}
		if n == 0 {
			return nil
		}
		if _, err := c.containers.StopHostContainers(ctx, tx, HostID(host), "the host's instance is ending"); err != nil {
			return err
		}
		return notifyMachines(ctx, tx, host)
	})
}

// hostGone ends a cloud host in phase from whose instance is gone; a host
// that left from since it was read is left alone. Its containers stop
// through execution in the same transaction. An empty failure marks a
// termination the fleet asked for.
func (c *Compute) hostGone(ctx context.Context, host uuid.UUID, from Phase, failure Failure, message string) error {
	to := PhaseFailed
	if failure == "" {
		to = PhaseDeleted
	}
	if err := transition(from, to); err != nil {
		return err
	}
	return inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		var n int64
		var err error
		if failure == "" {
			n, err = q.MarkHostDeleted(ctx, MarkHostDeletedParams{ID: host, FromPhase: string(from)})
		} else {
			n, err = q.FailHost(ctx, FailHostParams{ID: host, FromPhase: string(from), Failure: ptr(string(failure)), Message: message})
		}
		if err != nil {
			return fmt.Errorf("mark host gone: %w", err)
		}
		if n == 0 {
			return nil
		}
		if _, err := c.containers.StopHostContainers(ctx, tx, HostID(host), "the host's instance is gone"); err != nil {
			return err
		}
		return notifyMachines(ctx, tx, host)
	})
}

// cancelOrphanSpotRequests ends the fleet's persistent Spot requests whose
// host is gone, with every instance they launched. A request whose launch
// answer was lost still has a live host and is spared.
func (c *Compute) cancelOrphanSpotRequests(ctx context.Context, logger *slog.Logger, client *ec2.Client) error {
	pages := ec2.NewDescribeSpotInstanceRequestsPaginator(client, &ec2.DescribeSpotInstanceRequestsInput{
		Filters: []ec2types.Filter{
			{Name: aws.String("tag:" + tagFleet), Values: []string{c.fleet.Name}},
			{Name: aws.String("state"), Values: []string{"open", "active", "disabled"}},
		},
	})
	var requests []ec2types.SpotInstanceRequest
	for pages.HasMorePages() {
		page, err := pages.NextPage(ctx)
		if err != nil {
			return fmt.Errorf("describe spot requests: %w", err)
		}
		requests = append(requests, page.SpotInstanceRequests...)
	}
	hosts := map[string]uuid.UUID{}
	var tagged []uuid.UUID
	for _, r := range requests {
		for _, t := range r.Tags {
			if id, err := uuid.Parse(aws.ToString(t.Value)); err == nil && aws.ToString(t.Key) == tagHost {
				hosts[aws.ToString(r.SpotInstanceRequestId)] = id
				tagged = append(tagged, id)
			}
		}
	}
	if len(requests) == 0 {
		return nil
	}
	live, err := c.queries.LiveHosts(ctx, tagged)
	if err != nil {
		return fmt.Errorf("read live hosts: %w", err)
	}
	// A live host keeps the request it records, or any while it records
	// none, as when its launch's answer was lost.
	keeps := map[uuid.UUID]*string{}
	for _, h := range live {
		keeps[h.ID] = h.SpotRequestID
	}
	for _, r := range requests {
		request := aws.ToString(r.SpotInstanceRequestId)
		if host, ok := hosts[request]; ok {
			if recorded, alive := keeps[host]; alive && (recorded == nil || *recorded == request) {
				continue
			}
		}
		if err := endSpotRequest(ctx, client, request); err != nil {
			logger.ErrorContext(ctx, "end orphan spot request", "spot_request_id", request, "error", err)
			continue
		}
		logger.WarnContext(ctx, "orphan spot request cancelled", "spot_request_id", request)
	}
	return nil
}
