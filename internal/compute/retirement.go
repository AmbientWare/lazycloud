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

// serviceLostAfter is how long a ready cloud host may stay lost before its
// instance is replaced.
const serviceLostAfter = 5 * time.Minute

// RetireResult summarizes one retirement pass.
type RetireResult struct {
	Drained    int
	Terminated int
}

// Retire drains idle cloud hosts beyond each market's headroom floor and
// terminates draining hosts that run nothing. A market is an owner, region
// and purchase market; its oldest idle hosts are the ones kept. A
// disconnecting account's hosts all drain.
func (c *Compute) Retire(ctx context.Context, logger *slog.Logger) (RetireResult, error) {
	var result RetireResult
	var terminate []ClaimTerminationsRow
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		result, terminate = RetireResult{}, nil
		q := c.queries.WithTx(tx)
		if err := q.MarkIdle(ctx); err != nil {
			return fmt.Errorf("mark idle hosts: %w", err)
		}
		idle, err := q.IdleHosts(ctx)
		if err != nil {
			return fmt.Errorf("list idle hosts: %w", err)
		}
		kept := map[string]int{}
		var drain []uuid.UUID
		for _, h := range idle {
			market := h.Owner + "/" + h.Region + "/" + h.Market
			if kept[market] < c.fleet.HeadroomFloor {
				kept[market]++
				continue
			}
			if h.IdleSince != nil && time.Since(*h.IdleSince) >= c.fleet.IdleTimeout {
				drain = append(drain, h.ID)
			}
		}
		if len(drain) > 0 {
			drained, err := q.DrainHosts(ctx, DrainHostsParams{Ids: drain, Reason: "idle"})
			if err != nil {
				return fmt.Errorf("drain idle hosts: %w", err)
			}
			result.Drained += len(drained)
		}
		disconnecting, err := q.DrainConnectionHosts(ctx)
		if err != nil {
			return fmt.Errorf("drain disconnecting hosts: %w", err)
		}
		for _, h := range disconnecting {
			if err := c.containers.DrainHostContainers(ctx, tx, HostID(h)); err != nil {
				return err
			}
		}
		result.Drained += len(disconnecting)
		if err := q.CancelConnectionLaunches(ctx); err != nil {
			return fmt.Errorf("cancel launches: %w", err)
		}
		if terminate, err = q.ClaimTerminations(ctx); err != nil {
			return fmt.Errorf("claim terminations: %w", err)
		}
		return nil
	})
	if err != nil {
		return RetireResult{}, fmt.Errorf("retire hosts: %w", err)
	}
	for _, h := range terminate {
		scope, err := c.scopeOf(ctx, h.ConnectionID)
		if err == nil {
			err = c.terminate(ctx, scope, h.Region, deref(h.InstanceID))
		}
		if err != nil {
			// Reconciliation terminates it again.
			logger.ErrorContext(ctx, "terminate host", "host_id", h.ID, "error", err)
			continue
		}
		result.Terminated++
		logger.InfoContext(ctx, "host terminating", "host_id", h.ID, "instance_id", deref(h.InstanceID))
	}
	return result, nil
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

// Reconcile compares cloud hosts with what EC2 reports for the fleet tag in
// each owner's regions. A host whose instance is gone or stopped fails and
// its containers stop through execution; a launched instance that never
// enrolled, or a ready host lost for long, is replaced; instances no host
// wants are terminated.
func (c *Compute) Reconcile(ctx context.Context, logger *slog.Logger) error {
	pairs, err := c.queries.FleetRegions(ctx)
	if err != nil {
		return fmt.Errorf("list fleet regions: %w", err)
	}
	for _, p := range pairs {
		if err := c.reconcileRegion(ctx, logger, p.ConnectionID, p.Region); err != nil {
			if ctx.Err() != nil {
				return err
			}
			logger.ErrorContext(ctx, "reconcile region", "connection_id", p.ConnectionID, "region", p.Region, "error", err)
		}
	}
	return nil
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
	hosts, err := c.queries.FleetHostsInRegion(ctx, FleetHostsInRegionParams{Region: region, ConnectionID: connection})
	if err != nil {
		return fmt.Errorf("list region hosts: %w", err)
	}
	wanted := map[string]bool{}
	for _, h := range hosts {
		if h.InstanceID == nil {
			continue
		}
		instance := *h.InstanceID
		wanted[instance] = true
		o, seen := observed[instance]
		gone := !seen || o.state == ec2types.InstanceStateNameTerminated || o.state == ec2types.InstanceStateNameShuttingDown
		stopped := seen && (o.state == ec2types.InstanceStateNameStopped || o.state == ec2types.InstanceStateNameStopping)
		phase := Phase(h.Phase)
		switch {
		case phase == PhaseTerminating && gone:
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
		case phase == PhaseProvisioning && o.state == ec2types.InstanceStateNameRunning:
			_, err = c.queries.SetHostPhase(ctx, SetHostPhaseParams{
				ID: h.ID, FromPhase: string(PhaseProvisioning), Phase: string(PhaseBooting), Message: PhaseBooting.Message(),
			})
		case phase == PhaseReady && HostState(h.State) == HostLost && h.LastSeenAt != nil &&
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
	return pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
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
