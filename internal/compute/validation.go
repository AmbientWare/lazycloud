package compute

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"math"
	"slices"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/cloudformation"
	cftypes "github.com/aws/aws-sdk-go-v2/service/cloudformation/types"
	"github.com/aws/aws-sdk-go-v2/service/sts"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// validationFailure is why an authorization failed its check.
type validationFailure struct {
	code    AuthorizationError
	message string
	// step is where the setup's stack stands when the failure is the
	// customer still setting it up, so a connection without a working
	// authorization keeps waiting.
	step *SetupStep
}

// setupFailure is a failure of an authorization that has never validated,
// at step.
func setupFailure(step SetupStep, code AuthorizationError, format string, args ...any) *validationFailure {
	f := fail(code, format, args...)
	f.step = &step
	return f
}

func (f *validationFailure) Error() string { return f.message }

func fail(code AuthorizationError, format string, args ...any) *validationFailure {
	return &validationFailure{code: code, message: truncate(fmt.Sprintf(format, args...))}
}

// maxMessage bounds the error and phase messages stored for users.
const maxMessage = 512

func truncate(s string) string {
	if len(s) <= maxMessage {
		return s
	}
	return s[:maxMessage]
}

// validated is what a passing check learned.
type validated struct {
	stackID         string
	nodeRoleARN     string
	instanceProfile string
}

// Validate checks the pending authorization, or the active one without a
// pending one, now. A failed check is not an error: the authorization
// carries its code and message.
func (c *Compute) Validate(ctx context.Context, account identity.UserID) (Connection, error) {
	row, err := c.queries.ConnectionOfAccount(ctx, uuid.UUID(account))
	if errors.Is(err, pgx.ErrNoRows) {
		return Connection{}, &NotFoundError{Message: "AWS account connection not found"}
	}
	if err != nil {
		return Connection{}, fmt.Errorf("read connection: %w", err)
	}
	if err := c.validateConnection(ctx, row.ID, true); err != nil {
		return Connection{}, err
	}
	return c.connectionByID(ctx, row.ID)
}

// validateConnection runs one validation. The connection moves to
// validating in one transaction, AWS is checked outside any, and the
// outcome applies only if no other change bumped the revision meanwhile.
func (c *Compute) validateConnection(ctx context.Context, id uuid.UUID, manual bool) error {
	var target Authorization
	var conn Connection
	var revision int32
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, err := q.LockConnection(ctx, id)
		if errors.Is(err, pgx.ErrNoRows) {
			return &NotFoundError{Message: "AWS account connection not found"}
		}
		if err != nil {
			return fmt.Errorf("lock connection: %w", err)
		}
		auths, err := q.ConnectionAuthorizations(ctx, id)
		if err != nil {
			return fmt.Errorf("read authorizations: %w", err)
		}
		conn = connectionOf(row, auths)
		switch conn.Phase {
		case ConnDraining, ConnRevoking, ConnVerifying, ConnActionRequire, ConnRetiring:
			return &ConflictError{Message: "AWS account connection is being removed"}
		case ConnValidating:
			if manual {
				return &ConflictError{Message: "AWS account connection validation is already running"}
			}
		case ConnAwaiting, ConnReady, ConnDegraded, ConnReconnecting:
		}
		switch {
		case conn.Pending != nil:
			target = *conn.Pending
		case conn.Active != nil:
			target = *conn.Active
		default:
			return &ConflictError{Message: "AWS account connection has no authorization to validate"}
		}
		if err := q.StartValidation(ctx, target.ID); err != nil {
			return fmt.Errorf("start validation: %w", err)
		}
		phase := ConnValidating
		if conn.Pending != nil && conn.Active != nil {
			phase = ConnReconnecting
		}
		updated, err := q.SetConnectionPhase(ctx, SetConnectionPhaseParams{
			ID: id, Phase: string(phase), StepAttempts: row.StepAttempts,
			NextStepAt: ptr(time.Now().Add(5 * time.Minute)),
		})
		if err != nil {
			return fmt.Errorf("set connection phase: %w", err)
		}
		revision = updated.Revision
		return nil
	})
	if err != nil {
		return fmt.Errorf("validate connection: %w", err)
	}

	result, checkErr := c.checkAuthorization(ctx, conn, target)
	var failure *validationFailure
	if checkErr != nil && !errors.As(checkErr, &failure) {
		failure = fail(ErrUpstreamUnavailabl, "%v", checkErr)
	}

	err = pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, err := q.LockConnection(ctx, id)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("lock connection: %w", err)
		}
		if row.Revision != revision {
			return &ConflictError{Message: "AWS account connection validation was superseded"}
		}
		attempts := int(row.StepAttempts) + 1
		if failure != nil {
			if err := q.AuthorizationFailed(ctx, AuthorizationFailedParams{
				ID: target.ID, ErrorCode: ptr(string(failure.code)), ErrorMessage: &failure.message, SetupStep: (*string)(failure.step),
			}); err != nil {
				return fmt.Errorf("record failure: %w", err)
			}
			phase := ConnDegraded
			switch {
			case conn.Pending != nil && target.ID == conn.Pending.ID && conn.Active != nil:
				phase = ConnReconnecting
			case conn.Active == nil && (failure.step != nil || failure.code == ErrAssumeRoleDenied || failure.code == ErrUpstreamUnavailabl):
				phase = ConnAwaiting
			}
			return setPhase(ctx, q, id, phase, ptr(time.Now().Add(nextCheck(target, failure, attempts))), attempts, nil)
		}
		if err := q.AuthorizationValidated(ctx, AuthorizationValidatedParams{
			ID: target.ID, StackID: nilIfEmpty(result.stackID),
			NodeRoleArn: nilIfEmpty(result.nodeRoleARN), NodeInstanceProfile: nilIfEmpty(result.instanceProfile),
		}); err != nil {
			return fmt.Errorf("record validation: %w", err)
		}
		if conn.Pending == nil || target.ID != conn.Pending.ID {
			return setPhase(ctx, q, id, ConnReady, nil, 0, nil)
		}
		if conn.Active != nil {
			if err := q.SetAuthorizationPhase(ctx, SetAuthorizationPhaseParams{ID: conn.Active.ID, Phase: string(AuthRetiring), Slot: ptr(string(slotRetiring))}); err != nil {
				return fmt.Errorf("retire active authorization: %w", err)
			}
		}
		if err := q.SetAuthorizationPhase(ctx, SetAuthorizationPhaseParams{ID: target.ID, Phase: string(AuthReady), Slot: ptr(string(slotActive))}); err != nil {
			return fmt.Errorf("activate authorization: %w", err)
		}
		if conn.Active != nil {
			return setPhase(ctx, q, id, ConnRetiring, now(), 0, nil)
		}
		if err := setPhase(ctx, q, id, ConnReady, nil, 0, nil); err != nil {
			return err
		}
		return notifyChannel(ctx, tx, id)
	})
	if err != nil {
		return fmt.Errorf("record validation: %w", err)
	}
	return nil
}

func nilIfEmpty(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// checkAuthorization proves the platform can act through the role: it
// assumes the role with the external ID, confirms the role refuses
// AssumeRole without it, and reads the stack's outputs.
func (c *Compute) checkAuthorization(ctx context.Context, conn Connection, a Authorization) (validated, error) {
	clients := c.aws()
	session := "lazycloud-validate-" + conn.ID.String()[:8]
	assumed := clients.assume(a.RoleARN, a.externalID, session, conn.ID.String())
	identity, err := clients.sts(assumed, a.Region).GetCallerIdentity(ctx, &sts.GetCallerIdentityInput{})
	if err != nil {
		if (accessDenied(err) || awsCode(err) == "") && a.SetupStep != nil {
			// The stack makes the role early and a rollback deletes it, so
			// a role refused after the stack was seen means the stack failed.
			// A failed stack keeps its name, so the console link cannot
			// create it again until it is deleted.
			if *a.SetupStep == SetupWaitingForStack {
				return validated{}, setupFailure(SetupWaitingForStack, ErrAssumeRoleDenied,
					"AWS refused AssumeRole on %s: stack %s is not created yet: %s", a.RoleARN, a.StackName, describeAWSError(err))
			}
			if *a.SetupStep == SetupStackFailed && a.ErrorCode != nil {
				// The reason recorded when the stack was seen failing stays.
				return validated{}, setupFailure(SetupStackFailed, *a.ErrorCode, "%s", a.ErrorMessage)
			}
			return validated{}, setupFailure(SetupStackFailed, ErrAssumeRoleDenied,
				"stack %s failed and AWS deleted its role; delete the stack in CloudFormation, then create it again", a.StackName)
		}
		if accessDenied(err) || awsCode(err) == "" {
			return validated{}, fail(ErrAssumeRoleDenied, "AWS refused AssumeRole on %s with its external ID: %s", a.RoleARN, describeAWSError(err))
		}
		return validated{}, fail(ErrUpstreamUnavailabl, "AWS STS: %s", describeAWSError(err))
	}
	if aws.ToString(identity.Account) != conn.AWSAccountID {
		return validated{}, fail(ErrAccountMismatch, "AWS assumed-role identity does not match the connected account and role")
	}
	for _, wrong := range []string{"", "incorrect-" + a.externalID[:8]} {
		probe := clients.assume(a.RoleARN, wrong, session+"-probe", conn.ID.String())
		if wrong == "" {
			probe = c.assumeWithoutExternalID(a.RoleARN, session+"-probe")
		}
		if _, err := clients.sts(probe, a.Region).GetCallerIdentity(ctx, &sts.GetCallerIdentityInput{}); err == nil {
			return validated{}, fail(ErrExternalIDOpen, "AWS authorization role accepted AssumeRole without the exact external ID")
		}
	}
	if a.Mode == ModeManagedStack {
		return c.checkStack(ctx, clients, assumed, a)
	}
	return validated{
		instanceProfile: existingRoleNodeProfile,
		nodeRoleARN:     fmt.Sprintf("arn:aws:iam::%s:role/%s", accountOfRole(a.RoleARN), existingRoleNodeProfile),
	}, nil
}

func (c *Compute) assumeWithoutExternalID(role, session string) awsScope {
	return awsScope{credentials: aws.NewCredentialsCache(stsAssume{client: c.sts, role: role, session: session})}
}

// stsAssume assumes a role without an external ID, to prove the role
// demands one.
type stsAssume struct {
	client  *sts.Client
	role    string
	session string
}

func (s stsAssume) Retrieve(ctx context.Context) (aws.Credentials, error) {
	out, err := s.client.AssumeRole(ctx, &sts.AssumeRoleInput{RoleArn: aws.String(s.role), RoleSessionName: aws.String(s.session)})
	if err != nil {
		return aws.Credentials{}, fmt.Errorf("assume role: %w", err)
	}
	return aws.Credentials{
		AccessKeyID: aws.ToString(out.Credentials.AccessKeyId), SecretAccessKey: aws.ToString(out.Credentials.SecretAccessKey),
		SessionToken: aws.ToString(out.Credentials.SessionToken), CanExpire: true, Expires: aws.ToTime(out.Credentials.Expiration),
	}, nil
}

func (c *Compute) checkStack(ctx context.Context, clients awsClients, scope awsScope, a Authorization) (validated, error) {
	if a.TemplateVersion != TemplateVersion || a.TemplateSHA256 != TemplateSHA256() {
		return validated{}, fail(ErrStackDrift, "AWS managed authorization uses an obsolete connection template; reconnect it")
	}
	out, err := clients.cloudFormation(scope, a.Region).DescribeStacks(ctx, &cloudformation.DescribeStacksInput{StackName: aws.String(a.StackName)})
	if err != nil {
		if accessDenied(err) {
			return validated{}, fail(ErrPermissionDrift, "AWS refused DescribeStacks: %s", describeAWSError(err))
		}
		return validated{}, fail(ErrStackDrift, "authorization stack %s was not found: %s", a.StackName, describeAWSError(err))
	}
	if len(out.Stacks) != 1 {
		return validated{}, fail(ErrStackDrift, "authorization stack %s was not found", a.StackName)
	}
	stack := out.Stacks[0]
	if a.SetupStep != nil && stack.StackStatus == cftypes.StackStatusCreateInProgress {
		return validated{}, setupFailure(SetupStackCreating, ErrStackDrift, "authorization stack %s is still being created", a.StackName)
	}
	if a.SetupStep != nil && stack.StackStatus != cftypes.StackStatusCreateComplete {
		return validated{}, setupFailure(SetupStackFailed, ErrStackDrift, "stack %s failed (%s): %s", a.StackName, stack.StackStatus,
			stackFailure(ctx, clients.cloudFormation(scope, a.Region), stack))
	}
	if stack.StackStatus != cftypes.StackStatusCreateComplete && stack.StackStatus != cftypes.StackStatusUpdateComplete {
		return validated{}, fail(ErrStackDrift, "authorization stack is not ready (%s)", stack.StackStatus)
	}
	outputs := map[string]string{}
	for _, o := range stack.Outputs {
		outputs[aws.ToString(o.OutputKey)] = aws.ToString(o.OutputValue)
	}
	if outputs["ConnectionRoleArn"] != a.RoleARN {
		return validated{}, fail(ErrStackDrift, "authorization stack outputs another role")
	}
	return validated{
		stackID: aws.ToString(stack.StackId), nodeRoleARN: outputs["NodeRoleArn"], instanceProfile: outputs["NodeInstanceProfileName"],
	}, nil
}

// advanceConnection runs a connection's due background step.
func (c *Compute) advanceConnection(ctx context.Context, id uuid.UUID) error {
	conn, err := c.connectionByID(ctx, id)
	var missing *NotFoundError
	if errors.As(err, &missing) {
		return nil
	}
	if err != nil {
		return err
	}
	switch conn.Phase {
	case ConnAwaiting, ConnReconnecting, ConnDegraded, ConnValidating:
		if p := conn.Pending; p != nil && p.expiresAt != nil && p.expiresAt.Before(time.Now()) {
			return c.settle(ctx, conn, c.expirePending(conn))
		}
		err := c.validateConnection(ctx, id, false)
		var superseded *ConflictError
		if errors.As(err, &superseded) {
			return nil
		}
		return err
	case ConnRetiring:
		return c.settle(ctx, conn, c.retire(ctx, conn))
	case ConnDraining:
		return c.settle(ctx, conn, c.drain(ctx, conn))
	case ConnRevoking, ConnVerifying:
		return c.settle(ctx, conn, c.revoke(ctx, conn))
	case ConnReady, ConnActionRequire:
	}
	return nil
}

// accountOfRole is the account id in a role ARN.
func accountOfRole(arn string) string {
	if m := roleARNPattern.FindStringSubmatch(arn); m != nil {
		return m[2]
	}
	return ""
}

// setupChecks is how long after it starts a setup is checked every few
// seconds; later checks back off. CloudFormation creates the stack in about
// three minutes once the customer starts it.
const setupChecks = 30 * time.Minute

// nextCheck is when a failed validation runs again: soon while the
// customer sets up the stack, so the connection turns ready within seconds
// of the stack, and otherwise backing off up to five minutes apart.
func nextCheck(a Authorization, failure *validationFailure, attempts int) time.Duration {
	if failure.step != nil && time.Since(a.CreatedAt) < setupChecks {
		if *failure.step == SetupStackCreating {
			return 10 * time.Second
		}
		return 15 * time.Second
	}
	return time.Duration(math.Min(math.Pow(2, float64(attempts-1)), 300)) * time.Second
}

// stackFailure is why CloudFormation failed to create stack: its earliest
// failed event, or the stack's own reason when its events cannot be read.
func stackFailure(ctx context.Context, cf *cloudformation.Client, stack cftypes.Stack) string {
	reason := cmp.Or(aws.ToString(stack.StackStatusReason), string(stack.StackStatus))
	out, err := cf.DescribeStackEvents(ctx, &cloudformation.DescribeStackEventsInput{StackName: stack.StackId})
	if err != nil {
		return reason
	}
	// Events are newest first; the earliest failure caused the rollback.
	for _, e := range slices.Backward(out.StackEvents) {
		if e.ResourceStatus != cftypes.ResourceStatusCreateFailed {
			continue
		}
		if resource := aws.ToString(e.LogicalResourceId); resource != aws.ToString(stack.StackName) {
			return resource + ": " + aws.ToString(e.ResourceStatusReason)
		}
		return aws.ToString(e.ResourceStatusReason)
	}
	return reason
}
