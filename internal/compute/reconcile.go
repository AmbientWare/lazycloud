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

// Reconcile compares cloud hosts with what EC2 reports for the fleet tag in
// each owner's regions. A host whose instance is gone or stopped fails and
// its containers stop through execution; a launched instance that never
// enrolled, or a ready host lost for long, is replaced; instances no host
// wants are terminated.
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
				o := observedInstance{state: i.State.Name}
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
		terminated := !seen || o.state == ec2types.InstanceStateNameTerminated
		stopped := seen && (o.state == ec2types.InstanceStateNameStopped || o.state == ec2types.InstanceStateNameStopping)
		updating := h.UpdatingUntil != nil && time.Now().Before(*h.UpdatingUntil)
		phase := Phase(h.Phase)
		switch {
		case phase == PhaseTerminating && terminated:
			err = c.hostGone(ctx, h.ID, "", "")
		case phase == PhaseTerminating && o.state == ec2types.InstanceStateNameShuttingDown:
			// Terminating; EC2 confirms it on a later pass.
			err = c.hostGone(ctx, h.ID, "", "")
		case phase == PhaseTerminating:
			err = c.terminate(ctx, scope, region, instance)
		case gone:
			err = c.hostGone(ctx, h.ID, FailureProviderGone, "The provider terminated the instance")
		case stopped:
			if err = c.hostGone(ctx, h.ID, FailureProviderStopped, "The provider stopped the instance"); err == nil {
				err = c.terminate(ctx, scope, region, instance)
			}
		case (phase == PhaseProvisioning || phase == PhaseBooting) && h.LaunchedAt != nil &&
			time.Since(*h.LaunchedAt) > c.fleet.BootTimeout:
			if err = c.hostGone(ctx, h.ID, FailureBootstrapTimedOut, "The instance did not enroll in time"); err == nil {
				err = c.terminate(ctx, scope, region, instance)
			}
		case phase == PhaseJoining && time.Since(h.PhaseAt) > c.fleet.BootTimeout:
			if err = c.hostGone(ctx, h.ID, FailureEnrollment, "The agent enrolled but never opened a session"); err == nil {
				err = c.terminate(ctx, scope, region, instance)
			}
		case phase == PhaseProvisioning && o.state == ec2types.InstanceStateNameRunning:
			_, err = changePhase(ctx, c.queries, h.ID, PhaseProvisioning, PhaseBooting)
		case phase == PhaseReady && HostState(h.State) == HostLost && !updating && h.LastSeenAt != nil &&
			time.Since(*h.LastSeenAt) > serviceLostAfter:
			if err = c.hostGone(ctx, h.ID, FailureServiceLost, "The agent stopped reporting"); err == nil {
				err = c.terminate(ctx, scope, region, instance)
			}
		}
		if err != nil {
			logger.ErrorContext(ctx, "reconcile host", "host_id", h.ID, "instance_id", instance, "error", err)
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
	// A launch whose answer was lost still has a live host: spare it.
	known, err := c.queries.KnownHostIDs(ctx, tagged)
	if err != nil {
		return fmt.Errorf("read known hosts: %w", err)
	}
	live := map[string]bool{}
	for _, id := range known {
		live[id.String()] = true
	}
	for _, instance := range orphans {
		if live[observed[instance].host] {
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

// hostGone ends a cloud host whose instance is gone or must go. Its
// containers stop through execution in the same transaction. An empty
// failure marks a termination the fleet asked for.
func (c *Compute) hostGone(ctx context.Context, host uuid.UUID, failure Failure, message string) error {
	return inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		if failure == "" {
			if err := q.MarkHostDeleted(ctx, host); err != nil {
				return fmt.Errorf("mark host deleted: %w", err)
			}
		} else if err := q.FailHost(ctx, FailHostParams{ID: host, Failure: ptr(string(failure)), Message: message}); err != nil {
			return fmt.Errorf("fail host: %w", err)
		}
		if _, err := c.containers.StopHostContainers(ctx, tx, HostID(host), "the host's instance is gone"); err != nil {
			return err
		}
		return notifyMachines(ctx, tx, host)
	})
}
