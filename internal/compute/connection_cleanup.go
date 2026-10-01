package compute

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/cloudformation"
	cftypes "github.com/aws/aws-sdk-go-v2/service/cloudformation/types"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/database"
)

// cleanupAttempts bounds automatic stack removal before the customer is
// asked to finish it.
const cleanupAttempts = 60

func (c *Compute) aws() awsClients {
	return awsClients{base: c.fleet.AWS, endpoints: c.fleet.Endpoints}
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

// expirePending drops a setup the customer never finished: without an
// active authorization the connection goes, otherwise the active one keeps
// serving.
func (c *Compute) expirePending(ctx context.Context, conn Connection) error {
	return inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		if conn.Active == nil {
			return q.DeleteConnection(ctx, conn.ID)
		}
		if err := q.SetAuthorizationPhase(ctx, SetAuthorizationPhaseParams{ID: conn.Pending.ID, Phase: string(AuthRetired)}); err != nil {
			return fmt.Errorf("retire pending authorization: %w", err)
		}
		return setPhase(ctx, q, conn.ID, ConnReady, nil, 0, [2]string{})
	})
}

// finishDrain moves a disconnecting connection to revoking once its
// instances are gone; retirement drains them meanwhile.
func (c *Compute) finishDrain(ctx context.Context, conn Connection) error {
	return inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		live, err := q.ConnectionLiveHosts(ctx, &conn.ID)
		if err != nil {
			return fmt.Errorf("count live hosts: %w", err)
		}
		if live > 0 {
			return setPhase(ctx, q, conn.ID, ConnDraining, ptr(time.Now().Add(10*time.Second)), 0, [2]string{})
		}
		return setPhase(ctx, q, conn.ID, ConnRevoking, now(), 0, [2]string{})
	})
}

// removeStack deletes an authorization's stack through the active role and
// waits for CloudFormation to finish. Retiring a replaced authorization
// returns the connection to after; revoking the active one deletes the
// connection. An existing role has no stack and is left to its owner.
func (c *Compute) removeStack(ctx context.Context, conn Connection, a *Authorization, after ConnectionPhase) error {
	if a == nil || conn.Active == nil {
		return c.finishRemoval(ctx, conn, a, after)
	}
	if a.Mode == ModeExistingRole || a.StackName == "" {
		return c.finishRemoval(ctx, conn, a, after)
	}
	clients := c.aws()
	scope := clients.assume(conn.Active.RoleARN, conn.Active.externalID, "lazycloud-cleanup-"+conn.ID.String()[:8], conn.ID.String())
	cf := clients.cloudFormation(scope, a.Region)
	out, err := cf.DescribeStacks(ctx, &cloudformation.DescribeStacksInput{StackName: aws.String(a.StackName)})
	switch {
	case err != nil && (awsCode(err) == "ValidationError" || accessDenied(err) || awsCode(err) == ""):
		// Gone: the stack does not exist, or it took the role with it.
		return c.finishRemoval(ctx, conn, a, after)
	case err != nil:
		return c.cleanupLater(ctx, conn, fmt.Errorf("describe stack: %w", err))
	}
	if len(out.Stacks) == 0 {
		return c.finishRemoval(ctx, conn, a, after)
	}
	stack := out.Stacks[0]
	if stack.StackStatus == cftypes.StackStatusDeleteComplete {
		return c.finishRemoval(ctx, conn, a, after)
	}
	if stack.StackStatus == cftypes.StackStatusDeleteInProgress {
		return c.cleanupLater(ctx, conn, nil)
	}
	if stack.StackStatus == cftypes.StackStatusDeleteFailed {
		return c.needsCustomer(ctx, conn, a, aws.ToString(stack.StackId))
	}
	if _, err := cf.DeleteStack(ctx, &cloudformation.DeleteStackInput{StackName: aws.String(a.StackName)}); err != nil {
		if accessDenied(err) {
			return c.needsCustomer(ctx, conn, a, aws.ToString(stack.StackId))
		}
		return c.cleanupLater(ctx, conn, fmt.Errorf("delete stack: %w", err))
	}
	return inTx(ctx, c, func(tx pgx.Tx) error {
		next := conn.Phase
		if next == ConnRevoking {
			next = ConnVerifying
		}
		return setPhase(ctx, c.queries.WithTx(tx), conn.ID, next, ptr(time.Now().Add(10*time.Second)), 0, [2]string{})
	})
}

// cleanupLater schedules the next cleanup step with backoff, and hands the
// cleanup to the customer after cleanupAttempts.
func (c *Compute) cleanupLater(ctx context.Context, conn Connection, cause error) error {
	return inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, err := q.LockConnection(ctx, conn.ID)
		if err != nil {
			return fmt.Errorf("lock connection: %w", err)
		}
		attempts := int(row.StepAttempts) + 1
		if attempts > cleanupAttempts {
			return setPhase(ctx, q, conn.ID, ConnActionRequire, nil, attempts, [2]string{consoleURL(connectionRegion, ""), "Review AWS cleanup"})
		}
		delay := min(time.Duration(10<<min(attempts-1, 4))*time.Second, 5*time.Minute)
		if err := setPhase(ctx, q, conn.ID, ConnectionPhase(row.Phase), ptr(time.Now().Add(delay)), attempts, [2]string{}); err != nil {
			return err
		}
		if cause != nil {
			return fmt.Errorf("connection cleanup: %w", cause)
		}
		return nil
	})
}

// needsCustomer asks the customer to delete the stack in AWS.
func (c *Compute) needsCustomer(ctx context.Context, conn Connection, a *Authorization, stackID string) error {
	return inTx(ctx, c, func(tx pgx.Tx) error {
		return setPhase(ctx, c.queries.WithTx(tx), conn.ID, ConnActionRequire, nil, 0,
			[2]string{consoleURL(a.Region, stackID), "Delete the stack in AWS"})
	})
}

// finishRemoval retires a replaced authorization, or deletes a revoked
// connection.
func (c *Compute) finishRemoval(ctx context.Context, conn Connection, a *Authorization, after ConnectionPhase) error {
	return inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		if after == "" {
			if live, err := q.ConnectionLiveHosts(ctx, &conn.ID); err != nil {
				return fmt.Errorf("count live hosts: %w", err)
			} else if live > 0 {
				return errors.New("the connection still has live hosts")
			}
			return q.DeleteConnection(ctx, conn.ID)
		}
		if a != nil {
			if err := q.SetAuthorizationPhase(ctx, SetAuthorizationPhaseParams{ID: a.ID, Phase: string(AuthRetired)}); err != nil {
				return fmt.Errorf("retire authorization: %w", err)
			}
		}
		return setPhase(ctx, q, conn.ID, after, nil, 0, [2]string{})
	})
}

func consoleURL(region, stackID string) string {
	u := fmt.Sprintf("https://%s.console.aws.amazon.com/cloudformation/home?region=%s#/stacks", region, region)
	if stackID != "" {
		u += "/stackinfo?stackId=" + url.QueryEscape(stackID)
	}
	return u
}
