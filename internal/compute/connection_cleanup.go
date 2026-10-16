package compute

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/cloudformation"
	cftypes "github.com/aws/aws-sdk-go-v2/service/cloudformation/types"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/database"
)

const (
	// cleanupAttempts bounds the steps a draining, removal or retirement
	// phase repeats before the customer is asked to finish it; with
	// settle's backoff they span about forty minutes.
	cleanupAttempts = 12
	// connectionStepLease bounds one connection step and holds the
	// connection for it against other scheduler replicas.
	connectionStepLease = 2 * time.Minute
)

func (c *Compute) aws() awsClients {
	return awsClients{base: c.fleet.AWS, endpoints: c.fleet.Endpoints, platform: c.sts}
}

// inTx runs fn in a transaction.
func inTx(ctx context.Context, c *Compute, fn func(pgx.Tx) error) error {
	if err := pgx.BeginFunc(ctx, c.pool, fn); err != nil {
		return fmt.Errorf("connection step: %w", err)
	}
	return nil
}

func notifyChannel(ctx context.Context, tx pgx.Tx, id uuid.UUID) error {
	return database.Notify(ctx, tx, ChannelCompute, id.String())
}

// customerAction is AWS refusing a step: only the customer can let it
// finish, in AWS at url.
type customerAction struct {
	url, label string
	cause      error
}

func (e *customerAction) Error() string { return e.label + ": " + describeAWSError(e.cause) }
func (e *customerAction) Unwrap() error { return e.cause }

var errRoleGone = errors.New("AWS refused AssumeRole on the connection role")

// outcome is how a connection step ended, for settle to write.
type outcome struct {
	// done moves the connection on in settle's transaction.
	done func(ctx context.Context, tx pgx.Tx, q *Queries) error
	// err is why the step failed; a *customerAction waits on the customer.
	err error
	// action is where the customer finishes a step that never does.
	action [2]string
	// until holds a wait, uncounted, while the step can still pass by then.
	until *time.Time
}

func moveTo(conn Connection, phase ConnectionPhase, next *time.Time) outcome {
	return outcome{done: func(ctx context.Context, _ pgx.Tx, q *Queries) error {
		return setPhase(ctx, q, conn.ID, phase, next, 0, [2]string{})
	}}
}

// roleRefused is a step whose role AWS would not let the platform assume:
// tried again after err, and without one, left to the customer.
func roleRefused(err error, action [2]string) outcome {
	if err == nil {
		err = &customerAction{url: action[0], label: action[1], cause: errRoleGone}
	}
	return outcome{err: err, action: action}
}

// settle writes a step's outcome under the connection's lock, unless the
// connection is gone or another change bumped its revision since the step
// read it. A step neither done nor refused runs again with backoff, and
// after cleanupAttempts waits on the customer.
func (c *Compute) settle(ctx context.Context, conn Connection, o outcome) error {
	var refused *customerAction
	err := inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, err := q.LockConnection(ctx, conn.ID)
		if errors.Is(err, pgx.ErrNoRows) || err == nil && int(row.Revision) != conn.Revision {
			return nil
		}
		if err != nil {
			return fmt.Errorf("lock connection: %w", err)
		}
		attempts := int(row.StepAttempts)
		switch {
		case o.done != nil:
			return o.done(ctx, tx, q)
		case errors.As(o.err, &refused):
			return setPhase(ctx, q, conn.ID, ConnActionRequire, nil, attempts, [2]string{cmp.Or(refused.url, o.action[0]), truncate(refused.label)})
		case o.until != nil:
			next := time.Now().Add(time.Minute)
			if o.until.Before(next) {
				next = *o.until
			}
			return setPhase(ctx, q, conn.ID, conn.Phase, &next, attempts, [2]string{})
		case attempts >= cleanupAttempts:
			return setPhase(ctx, q, conn.ID, ConnActionRequire, nil, attempts+1,
				cmp.Or(o.action, [2]string{stacksURL(connectionRegion), "Review LazyCloud's resources in AWS"}))
		}
		delay := min(time.Duration(10<<min(attempts, 5))*time.Second, 5*time.Minute)
		return setPhase(ctx, q, conn.ID, conn.Phase, ptr(time.Now().Add(delay)), attempts+1, [2]string{})
	})
	if err == nil && o.err != nil && refused == nil {
		err = fmt.Errorf("connection step: %w", o.err)
	}
	return err
}

// expirePending drops a setup the customer never finished: without an
// active authorization the connection is removed, otherwise the pending
// one is retired while the active one keeps serving.
func (c *Compute) expirePending(conn Connection) outcome {
	return outcome{done: func(ctx context.Context, tx pgx.Tx, q *Queries) error {
		if conn.Active == nil {
			_, err := beginRemoval(ctx, tx, q, conn)
			return err
		}
		return retirePending(ctx, tx, q, conn)
	}}
}

// retirePending moves the pending authorization to the retiring slot:
// retirement deletes its stack, if the customer created one, while the
// active authorization keeps serving.
func retirePending(ctx context.Context, tx pgx.Tx, q *Queries, conn Connection) error {
	if err := q.SetAuthorizationPhase(ctx, SetAuthorizationPhaseParams{ID: conn.Pending.ID, Phase: string(AuthRetiring), Slot: ptr("retiring")}); err != nil {
		return fmt.Errorf("retire pending authorization: %w", err)
	}
	if err := setPhase(ctx, q, conn.ID, ConnRetiring, now(), 0, [2]string{}); err != nil {
		return err
	}
	return notifyChannel(ctx, tx, conn.ID)
}

// drain waits for a disconnecting connection's hosts to go: retirement
// drains them and the actuator terminates them through the active role. A
// role AWS refuses leaves them to the customer.
func (c *Compute) drain(ctx context.Context, conn Connection) outcome {
	live, err := c.queries.ConnectionLiveHosts(ctx, &conn.ID)
	if err != nil {
		return outcome{err: fmt.Errorf("count live hosts: %w", err)}
	}
	if live == 0 {
		return moveTo(conn, ConnRevoking, now())
	}
	action := c.instancesAction(conn)
	if conn.Active != nil {
		if ok, err := assumable(ctx, c.roleScope(conn, conn.Active)); !ok {
			return roleRefused(err, action)
		}
	}
	return outcome{action: action}
}

// revoke removes a disconnected account from AWS through its access
// authorization: the active one, or for a setup that never finished, the
// pending one. It terminates every instance tagged as one of the
// connection's hosts, then deletes each stack of every generation not
// confirmed deleted, the access stack last since it holds the role, and
// deletes the connection once every stack is confirmed gone.
func (c *Compute) revoke(ctx context.Context, conn Connection) outcome {
	access := cmp.Or(conn.Active, conn.Pending)
	if access == nil {
		return c.finishRevoke(ctx, conn)
	}
	scope := c.roleScope(conn, access)
	if ok, err := assumable(ctx, scope); !ok {
		switch {
		case err != nil:
		case access.deleteRequested != nil:
			// The stack removes its role last: once the platform asked to
			// delete it, the role going is the stack going.
			if err := c.queries.StackDeleted(ctx, access.ID); err != nil {
				return outcome{err: fmt.Errorf("record stack deleted: %w", err)}
			}
			return c.finishRevoke(ctx, conn)
		case conn.Active == nil && access.expiresAt != nil && time.Now().Before(*access.expiresAt):
			// The customer may still be creating the setup's stack, which
			// makes the role.
			return outcome{until: access.expiresAt}
		case conn.Active == nil:
			return c.finishRevoke(ctx, conn)
		}
		return roleRefused(err, [2]string{stacksURL(access.Region), "Delete the LazyCloud connection stacks in AWS"})
	}
	if conn.Active != nil && conn.Phase == ConnRevoking {
		left, err := c.sweepInstances(ctx, scope, conn)
		if err != nil || left > 0 {
			return outcome{err: err, action: c.instancesAction(conn)}
		}
	}
	stacks, err := c.queries.RemainingStacks(ctx, conn.ID)
	if err != nil {
		return outcome{err: fmt.Errorf("read stacks: %w", err)}
	}
	var last, waiting *connectionStack
	for _, row := range stacks {
		s := connectionStack{authorization: row.ID, region: row.Region, name: row.StackName, id: deref(row.StackID)}
		if row.ID == access.ID {
			last = &s
			continue
		}
		gone, err := c.deleteStack(ctx, scope, &s)
		if err != nil {
			return outcome{err: err, action: s.action()}
		}
		if !gone {
			waiting = &s
		}
	}
	switch {
	case waiting != nil:
		return outcome{action: waiting.action()}
	case last == nil:
		return c.finishRevoke(ctx, conn)
	}
	gone, err := c.deleteStack(ctx, scope, last)
	var refused *customerAction
	switch {
	case errors.As(err, &refused) && access.deleteRequested != nil:
		// CloudFormation removes the role's policies just before the role;
		// a later step finds the role gone.
		return outcome{err: refused.cause, action: last.action()}
	case err != nil || !gone && conn.Phase == ConnVerifying:
		return outcome{err: err, action: last.action()}
	case !gone:
		return moveTo(conn, ConnVerifying, ptr(time.Now().Add(10*time.Second)))
	}
	return c.finishRevoke(ctx, conn)
}

// sweepInstances terminates the running instances tagged as hosts of conn
// in each of its regions, including those whose host already ended or
// whose launch answer was lost, and reports how many have not ended yet.
func (c *Compute) sweepInstances(ctx context.Context, scope awsScope, conn Connection) (int, error) {
	regions, err := c.queries.ConnectionRegions(ctx, conn.ID)
	if err != nil {
		return 0, fmt.Errorf("read connection regions: %w", err)
	}
	left := 0
	for _, region := range regions {
		client := c.aws().ec2(scope, region)
		hosts := map[uuid.UUID][]string{}
		pages := ec2.NewDescribeInstancesPaginator(client, &ec2.DescribeInstancesInput{
			Filters: []ec2types.Filter{{Name: aws.String("tag:" + tagFleet), Values: []string{c.fleet.Name}}},
		})
		for pages.HasMorePages() {
			page, err := pages.NextPage(ctx)
			if err != nil {
				return 0, refusedOr(err, "describe instances in %s", region)
			}
			for _, r := range page.Reservations {
				for _, i := range r.Instances {
					if i.State == nil || i.State.Name == ec2types.InstanceStateNameTerminated || i.State.Name == ec2types.InstanceStateNameShuttingDown {
						continue
					}
					for _, t := range i.Tags {
						if id, err := uuid.Parse(aws.ToString(t.Value)); err == nil && aws.ToString(t.Key) == tagHost {
							hosts[id] = append(hosts[id], aws.ToString(i.InstanceId))
						}
					}
				}
			}
		}
		ids := make([]uuid.UUID, 0, len(hosts))
		for id := range hosts {
			ids = append(ids, id)
		}
		owned, err := c.queries.ConnectionHostsAmong(ctx, ConnectionHostsAmongParams{Ids: ids, ConnectionID: &conn.ID})
		if err != nil {
			return 0, fmt.Errorf("read connection hosts: %w", err)
		}
		var instances []string
		for _, id := range owned {
			instances = append(instances, hosts[id]...)
		}
		if len(instances) == 0 {
			continue
		}
		left += len(instances)
		if _, err := client.TerminateInstances(ctx, &ec2.TerminateInstancesInput{InstanceIds: instances}); err != nil &&
			awsCode(err) != "InvalidInstanceID.NotFound" {
			return 0, refusedOr(err, "terminate instances in %s", region)
		}
	}
	return left, nil
}

// connectionStack is one authorization's stack.
type connectionStack struct {
	authorization uuid.UUID
	region, name  string
	id            string
}

// action shows the customer the stack in the CloudFormation console.
func (s connectionStack) action() [2]string {
	return [2]string{stackURL(s.region, s.id, s.name), "Delete the stack " + s.name + " in AWS"}
}

// deleteStack moves a connection stack toward deleted and reports whether
// CloudFormation no longer has it, which it records: only a stack it does
// not know or one whose deletion completed is gone. A failed deletion is
// asked again, so the caller's attempts bound it. AWS refusing the role's
// authority is a *customerAction. s takes the stack id CloudFormation
// reports.
func (c *Compute) deleteStack(ctx context.Context, scope awsScope, s *connectionStack) (bool, error) {
	cf := c.aws().cloudFormation(scope, s.region)
	out, err := cf.DescribeStacks(ctx, &cloudformation.DescribeStacksInput{StackName: aws.String(s.name)})
	gone := awsCode(err) == "ValidationError" // CloudFormation's answer for a name it has no live stack of
	switch {
	case gone:
	case err != nil:
		return false, refusedOr(err, "describe stack %s", s.name)
	case len(out.Stacks) != 1:
		return false, fmt.Errorf("describe stack %s: CloudFormation listed %d stacks", s.name, len(out.Stacks))
	default:
		s.id = aws.ToString(out.Stacks[0].StackId)
		gone = out.Stacks[0].StackStatus == cftypes.StackStatusDeleteComplete
	}
	if gone {
		if err := c.queries.StackDeleted(ctx, s.authorization); err != nil {
			return false, fmt.Errorf("record stack deleted: %w", err)
		}
		return true, nil
	}
	if out.Stacks[0].StackStatus == cftypes.StackStatusDeleteInProgress {
		return false, nil
	}
	if _, err := cf.DeleteStack(ctx, &cloudformation.DeleteStackInput{StackName: aws.String(s.name)}); err != nil {
		return false, refusedOr(err, "delete stack %s", s.name)
	}
	if err := c.queries.StackDeleteRequested(ctx, s.authorization); err != nil {
		return false, fmt.Errorf("record stack deletion: %w", err)
	}
	return false, nil
}

// refusedOr wraps a failed AWS call: AWS refusing the role's authority is
// a *customerAction, anything else may pass on a later attempt.
func refusedOr(err error, format string, args ...any) error {
	what := fmt.Sprintf(format, args...)
	if accessDenied(err) {
		return &customerAction{label: "AWS refused to " + what, cause: err}
	}
	return fmt.Errorf("%s: %w", what, err)
}

// roleScope assumes a's role for cleanup.
func (c *Compute) roleScope(conn Connection, a *Authorization) awsScope {
	return c.aws().assume(a.RoleARN, a.externalID, "lazycloud-cleanup-"+conn.ID.String()[:8], conn.ID.String())
}

// assumable reports whether AWS lets the platform assume scope's role. STS
// refusing it, as it does once the customer deleted the role, is false.
func assumable(ctx context.Context, scope awsScope) (bool, error) {
	if _, err := scope.credentials.Retrieve(ctx); err != nil {
		if accessDenied(err) {
			return false, nil
		}
		return false, fmt.Errorf("assume the connection role: %w", err)
	}
	return true, nil
}

// finishRevoke deletes a revoked connection once it has no live host,
// after the launch permission on the fleet's images.
func (c *Compute) finishRevoke(ctx context.Context, conn Connection) outcome {
	if err := c.unshareImages(ctx, conn); err != nil {
		return outcome{err: err}
	}
	live, err := c.queries.ConnectionLiveHosts(ctx, &conn.ID)
	if err == nil && live > 0 {
		err = fmt.Errorf("the connection still has %d live hosts", live)
	}
	if err != nil {
		return outcome{err: err, action: c.instancesAction(conn)}
	}
	return outcome{done: func(ctx context.Context, _ pgx.Tx, q *Queries) error {
		if err := q.DeleteConnection(ctx, conn.ID); err != nil {
			return fmt.Errorf("delete connection: %w", err)
		}
		return nil
	}}
}

// unshareImages removes the launch permission shareImage granted a
// connection's account on the fleet's baked images, unless another
// connection still reaches that account. A connection that never had an
// active authorization launched nothing.
func (c *Compute) unshareImages(ctx context.Context, conn Connection) error {
	if c.fleet.Images == nil || conn.Active == nil {
		return nil
	}
	shared, err := c.queries.AccountConnectedElsewhere(ctx, AccountConnectedElsewhereParams{AwsAccountID: conn.AWSAccountID, ID: conn.ID})
	if err != nil {
		return fmt.Errorf("read connections of the account: %w", err)
	}
	if shared {
		return nil
	}
	for _, byRegion := range []map[string]string{c.fleet.Images.CPU, c.fleet.Images.GPU} {
		for region, image := range byRegion {
			if image == "" || strings.HasPrefix(image, "resolve:ssm:") {
				continue
			}
			_, err := c.aws().ec2(awsScope{key: string(KindPlatform)}, region).ModifyImageAttribute(ctx, &ec2.ModifyImageAttributeInput{
				ImageId: aws.String(image),
				LaunchPermission: &ec2types.LaunchPermissionModifications{
					Remove: []ec2types.LaunchPermission{{UserId: aws.String(conn.AWSAccountID)}},
				},
			})
			if err != nil && !strings.HasPrefix(awsCode(err), "InvalidAMIID") {
				return fmt.Errorf("remove launch permission on %s: %w", image, err)
			}
		}
	}
	return nil
}

// retire removes a replaced or cancelled authorization once the hosts
// launched under it are gone, since its stack holds the role they run as.
// The stack is deleted through the active role; an existing role has no
// stack and is left to its owner.
func (c *Compute) retire(ctx context.Context, conn Connection) outcome {
	a := conn.Retiring
	if a != nil && conn.Active != nil {
		live, err := c.drainAuthorization(ctx, a.ID)
		if err != nil || live > 0 {
			return outcome{err: err, action: c.instancesAction(conn)}
		}
		if a.Mode == ModeManagedStack {
			s := connectionStack{authorization: a.ID, region: a.Region, name: a.StackName, id: a.StackID}
			scope := c.roleScope(conn, conn.Active)
			if ok, err := assumable(ctx, scope); !ok {
				return roleRefused(err, s.action())
			}
			if gone, err := c.deleteStack(ctx, scope, &s); err != nil || !gone {
				return outcome{err: err, action: s.action()}
			}
		}
	}
	return outcome{done: func(ctx context.Context, _ pgx.Tx, q *Queries) error {
		if a != nil {
			if err := q.SetAuthorizationPhase(ctx, SetAuthorizationPhaseParams{ID: a.ID, Phase: string(AuthRetired)}); err != nil {
				return fmt.Errorf("retire authorization: %w", err)
			}
		}
		return setPhase(ctx, q, conn.ID, ConnReady, nil, 0, [2]string{})
	}}
}

// drainAuthorization drains the hosts launched under an authorization and
// returns how many still exist.
func (c *Compute) drainAuthorization(ctx context.Context, authorization uuid.UUID) (int, error) {
	var live int
	err := inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		drained, err := q.DrainAuthorizationHosts(ctx, &authorization)
		if err != nil {
			return fmt.Errorf("drain hosts: %w", err)
		}
		for _, h := range drained {
			if err := c.containers.DrainHostContainers(ctx, tx, HostID(h)); err != nil {
				return err
			}
		}
		n, err := q.AuthorizationLiveHosts(ctx, &authorization)
		live = int(n)
		if err != nil {
			return fmt.Errorf("count hosts: %w", err)
		}
		return nil
	})
	return live, err
}

// instancesAction shows the customer the fleet's instances in the EC2
// console of the connection's region.
func (c *Compute) instancesAction(conn Connection) [2]string {
	region := connectionRegion
	if conn.Active != nil {
		region = conn.Active.Region
	}
	return [2]string{
		fmt.Sprintf("https://%s.console.aws.amazon.com/ec2/home?region=%s#Instances:tag:%s=%s", region, region, tagFleet, url.QueryEscape(c.fleet.Name)),
		"Terminate LazyCloud's instances in AWS",
	}
}

func instanceURL(region, instance string) string {
	return fmt.Sprintf("https://%s.console.aws.amazon.com/ec2/home?region=%s#InstanceDetails:instanceId=%s", region, region, url.QueryEscape(instance))
}

func stacksURL(region string) string {
	return fmt.Sprintf("https://%s.console.aws.amazon.com/cloudformation/home?region=%s#/stacks", region, region)
}

// stackURL opens a stack by id, or the stacks named name while its id is
// not known.
func stackURL(region, id, name string) string {
	if id != "" {
		return stacksURL(region) + "/stackinfo?stackId=" + url.QueryEscape(id)
	}
	return stacksURL(region) + "?filteringText=" + url.QueryEscape(name)
}
