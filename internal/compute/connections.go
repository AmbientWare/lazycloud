package compute

import (
	"cmp"
	"context"
	"crypto/rand"
	"crypto/sha256"
	_ "embed"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials/stscreds"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	s3types "github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

//go:embed connection_template.json
var connectionTemplate string

// TemplateVersion names the connection template; validation refuses stacks
// made from another one.
const TemplateVersion = "2026-10-16.v1"

// connectionRegion is where connection stacks are created. The template's
// subnets name this region's zone IDs, which are the same in every account.
const connectionRegion = "us-east-2"

// TemplateSHA256 is the hex digest of the connection template.
func TemplateSHA256() string {
	sum := sha256.Sum256([]byte(connectionTemplate))
	return hex.EncodeToString(sum[:])
}

// ConnectionPhase is an AWS connection's lifecycle.
type ConnectionPhase string

const (
	ConnAwaiting      ConnectionPhase = "awaiting_authorization"
	ConnValidating    ConnectionPhase = "validating"
	ConnReady         ConnectionPhase = "ready"
	ConnDegraded      ConnectionPhase = "degraded"
	ConnReconnecting  ConnectionPhase = "reconnect_pending"
	ConnRetiring      ConnectionPhase = "retiring_authorization"
	ConnDraining      ConnectionPhase = "disconnect_draining"
	ConnRevoking      ConnectionPhase = "revoking"
	ConnVerifying     ConnectionPhase = "verifying_revocation"
	ConnActionRequire ConnectionPhase = "action_required"
)

// Detail is the sentence users see for the phase.
func (p ConnectionPhase) Detail() string {
	switch p {
	case ConnAwaiting:
		return "Complete authorization in AWS."
	case ConnValidating:
		return "Checking AWS authorization."
	case ConnReady:
		return "AWS compute is available for your workspaces."
	case ConnDegraded:
		return "AWS authorization needs attention before new workloads can be placed."
	case ConnReconnecting:
		return "AWS compute remains available while replacement authorization is completed."
	case ConnRetiring:
		return "AWS compute remains available while previous authorization is removed."
	case ConnDraining:
		return "Removing AWS compute."
	case ConnRevoking:
		return "Revoking AWS authorization."
	case ConnVerifying:
		return "Verifying AWS removal."
	case ConnActionRequire:
		return "Automatic AWS cleanup needs attention before removal can finish."
	}
	return ""
}

// AuthorizationMode is how the customer granted the role.
type AuthorizationMode string

const (
	ModeManagedStack AuthorizationMode = "managed_stack"
	ModeExistingRole AuthorizationMode = "existing_role"
)

// AuthorizationPhase is one role generation's state.
type AuthorizationPhase string

const (
	AuthAwaiting   AuthorizationPhase = "awaiting_authorization"
	AuthValidating AuthorizationPhase = "validating"
	AuthReady      AuthorizationPhase = "ready"
	AuthDegraded   AuthorizationPhase = "degraded"
	AuthRetiring   AuthorizationPhase = "retiring"
	AuthRetired    AuthorizationPhase = "retired"
)

// AuthorizationError classifies a failed validation.
type AuthorizationError string

const (
	ErrAssumeRoleDenied   AuthorizationError = "assume_role_denied"
	ErrExternalIDOpen     AuthorizationError = "external_id_not_enforced"
	ErrAccountMismatch    AuthorizationError = "account_mismatch"
	ErrPermissionDrift    AuthorizationError = "permission_drift"
	ErrStackDrift         AuthorizationError = "stack_drift"
	ErrUpstreamUnavailabl AuthorizationError = "upstream_unavailable"
)

// ConnectionAction is what a user may do next.
type ConnectionAction string

const (
	ActionAuthorize       ConnectionAction = "authorize"
	ActionValidate        ConnectionAction = "validate"
	ActionReconnect       ConnectionAction = "reconnect"
	ActionCancelReconnect ConnectionAction = "cancel_reconnect"
	ActionRemove          ConnectionAction = "remove"
	ActionRetry           ConnectionAction = "retry"
	ActionForget          ConnectionAction = "forget"
)

// authorizationSlot is the place an authorization holds in its connection;
// a retired one holds none.
type authorizationSlot string

const (
	slotActive   authorizationSlot = "active"
	slotPending  authorizationSlot = "pending"
	slotRetiring authorizationSlot = "retiring"
)

// Authorization is one role generation of a connection.
type Authorization struct {
	ID                 uuid.UUID
	Generation         int
	Mode               AuthorizationMode
	Phase              AuthorizationPhase
	RoleARN            string
	Region             string
	StackName          string
	StackID            string
	TemplateVersion    string
	TemplateSHA256     string
	ErrorCode          *AuthorizationError
	ErrorMessage       string
	ValidationStarted  *time.Time
	LastValidated      *time.Time
	CreatedAt          time.Time
	UpdatedAt          time.Time
	externalID         string
	nodeRoleARN        string
	nodeInstanceProfle string
	networks           map[string]Network
	expiresAt          *time.Time
	// deleteRequested is when the platform last asked CloudFormation to
	// delete the stack.
	deleteRequested *time.Time
}

// Connection is an account's AWS connection as users see it.
type Connection struct {
	ID           uuid.UUID
	AWSAccountID string
	Phase        ConnectionPhase
	Revision     int
	Active       *Authorization
	Pending      *Authorization
	Retiring     *Authorization
	NextRetryAt  *time.Time
	// Customer is what the customer must do in AWS while the connection
	// waits on them.
	Customer  *CustomerStep
	CreatedAt time.Time
	UpdatedAt time.Time
	// ConsoleURL opens the pending managed authorization's stack, ready to
	// create, in the AWS Console while it waits for the customer. View sets
	// it.
	ConsoleURL       *string
	removalRequested *time.Time
}

// CustomerStep is what the customer does in AWS: Label, at URL.
type CustomerStep struct {
	URL, Label string
}

// HostsWorkloads reports whether new workloads can be placed in the account.
func (c Connection) HostsWorkloads() bool {
	return c.Active != nil && acceptsWork(c.Phase, c.Active.Phase)
}

// removing reports whether the connection is being disconnected, or its
// removal waits on the customer.
func (c Connection) removing() bool {
	return c.Phase == ConnDraining || c.Phase == ConnRevoking || c.Phase == ConnVerifying ||
		(c.Phase == ConnActionRequire && c.Retiring == nil)
}

// acceptsWork decides whether a connection in phase, whose active
// authorization is in active, takes new workspaces and hosts: the active
// authorization works and the connection is neither being removed nor
// waiting on the customer. HostingConnections applies the same rule in
// SQL.
func acceptsWork(phase ConnectionPhase, active AuthorizationPhase) bool {
	switch phase {
	case ConnDraining, ConnRevoking, ConnVerifying, ConnActionRequire:
		return false
	case ConnAwaiting, ConnValidating, ConnReady, ConnDegraded, ConnReconnecting, ConnRetiring:
	}
	return active == AuthReady
}

// awaitsStack reports whether the pending authorization waits for the
// customer to create its stack.
func (c Connection) awaitsStack() bool {
	waiting := c.Phase == ConnAwaiting || c.Phase == ConnReconnecting || c.Phase == ConnDegraded
	return waiting && c.Pending != nil && c.Pending.Mode == ModeManagedStack
}

// ManagesCapacity reports whether the platform still runs the account's
// existing instances.
func (c Connection) ManagesCapacity() bool {
	return c.HostsWorkloads() || c.Active != nil && c.Active.Phase == AuthReady &&
		(c.Phase == ConnDraining || c.Phase == ConnActionRequire && c.Retiring != nil)
}

// Actions lists what the user may do in the current phase.
func (c Connection) Actions() []ConnectionAction {
	switch c.Phase {
	case ConnAwaiting:
		return []ConnectionAction{ActionAuthorize, ActionValidate, ActionRemove}
	case ConnValidating, ConnRetiring:
		return []ConnectionAction{ActionRemove}
	case ConnReady:
		return []ConnectionAction{ActionReconnect, ActionRemove}
	case ConnDegraded:
		switch {
		case c.Active != nil:
			return []ConnectionAction{ActionReconnect, ActionValidate, ActionRemove}
		case c.awaitsStack():
			return []ConnectionAction{ActionAuthorize, ActionValidate, ActionRemove}
		}
		return []ConnectionAction{ActionValidate, ActionRemove}
	case ConnReconnecting:
		return []ConnectionAction{ActionAuthorize, ActionValidate, ActionCancelReconnect, ActionRemove}
	case ConnActionRequire:
		if c.Retiring != nil {
			return []ConnectionAction{ActionRetry, ActionRemove}
		}
		return []ConnectionAction{ActionRetry, ActionForget}
	case ConnDraining, ConnRevoking, ConnVerifying:
	}
	return []ConnectionAction{}
}

// ConnectRequest connects an AWS account.
type ConnectRequest struct {
	AWSAccountID string
	// RoleARN names an existing role; without it the customer creates the
	// managed stack.
	RoleARN    string
	Networks   map[string]Network
	ExternalID string
}

var awsRegionPattern = regexp.MustCompile(`^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$`)

var roleARNPattern = regexp.MustCompile(`^arn:(aws|aws-us-gov|aws-cn):iam::([0-9]{12}):role/[A-Za-z0-9+=,.@_/-]{1,512}$`)

// Connect creates the account's AWS connection with a pending
// authorization. Repeating an unfinished setup for the same account and
// role returns it again.
func (c *Compute) Connect(ctx context.Context, account identity.UserID, req ConnectRequest) (Connection, error) {
	if req.ExternalID != "" {
		// A caller-chosen external ID would let one account aim the platform
		// at a role another account set up.
		return Connection{}, &InvalidError{Message: "LazyCloud generates the external ID; make the role require the one it returns"}
	}
	if req.RoleARN != "" {
		m := roleARNPattern.FindStringSubmatch(req.RoleARN)
		if m == nil || m[2] != req.AWSAccountID {
			return Connection{}, &InvalidError{Message: "AWS role ARN must belong to account_id"}
		}
	} else if len(req.Networks) > 0 {
		return Connection{}, &InvalidError{Message: "AWS network may only be supplied with an existing role"}
	}
	for region := range req.Networks {
		if !awsRegionPattern.MatchString(region) {
			return Connection{}, &InvalidError{Message: fmt.Sprintf("AWS network key %q is not an AWS region such as us-east-2", region)}
		}
	}
	if c.fleet.PrincipalARN == "" {
		return Connection{}, &UnavailableError{Message: "AWS account connections are not configured on this platform"}
	}
	var id uuid.UUID
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		existing, err := q.LockConnectionOfAccount(ctx, uuid.UUID(account))
		switch {
		case errors.Is(err, pgx.ErrNoRows):
		case err != nil:
			return fmt.Errorf("lock connection: %w", err)
		default:
			auths, err := q.ConnectionAuthorizations(ctx, existing.ID)
			if err != nil {
				return fmt.Errorf("read authorizations: %w", err)
			}
			view := connectionOf(existing, auths)
			if view.removing() {
				return &ConflictError{Message: "this account's AWS connection is being removed; connect again once `lazycloud cloud status` shows none"}
			}
			if view.Active == nil && view.Pending != nil && existing.AwsAccountID == req.AWSAccountID &&
				(view.Pending.Mode == ModeExistingRole) == (req.RoleARN != "") &&
				(req.RoleARN == "" || view.Pending.RoleARN == req.RoleARN) {
				id = existing.ID
				return nil
			}
			return &ConflictError{Message: "this account already has an AWS account connection"}
		}
		if err := billing.AdmitConnectedCloud(ctx, tx, uuid.UUID(account)); err != nil {
			return err
		}
		if err := roleFree(ctx, q, req.RoleARN, nil); err != nil {
			return err
		}
		row, err := q.InsertConnection(ctx, InsertConnectionParams{AccountID: uuid.UUID(account), AwsAccountID: req.AWSAccountID})
		if err != nil {
			return fmt.Errorf("insert connection: %w", err)
		}
		id = row.ID
		return c.insertAuthorization(ctx, q, row, 1, req)
	})
	if err != nil {
		return Connection{}, fmt.Errorf("connect AWS: %w", err)
	}
	return c.connectionByID(ctx, id)
}

// insertAuthorization adds generation gen as the pending authorization.
func (c *Compute) insertAuthorization(ctx context.Context, q *Queries, conn CloudConnection, gen int, req ConnectRequest) error {
	externalID := req.ExternalID
	if externalID == "" {
		raw := make([]byte, 48)
		if _, err := rand.Read(raw); err != nil {
			return fmt.Errorf("generate external id: %w", err)
		}
		externalID = base64.RawURLEncoding.EncodeToString(raw)
	}
	networks, err := json.Marshal(nonNilMap(req.Networks))
	if err != nil {
		return fmt.Errorf("encode networks: %w", err)
	}
	params := InsertAuthorizationParams{
		ConnectionID: conn.ID, Generation: int32(gen), ExternalID: externalID, Region: connectionRegion, //nolint:gosec // Generations are small.
		Networks: networks,
	}
	if req.RoleARN != "" {
		params.Mode, params.RoleArn = string(ModeExistingRole), req.RoleARN
		params.NodeInstanceProfile = ptr(existingRoleNodeProfile)
	} else {
		suffix := connectionSuffix(conn)
		stack := fmt.Sprintf("%s-g%d", stackPrefix(suffix), gen)
		params.Mode, params.StackName = string(ModeManagedStack), &stack
		params.RoleArn = fmt.Sprintf("arn:aws:iam::%s:role/%s", conn.AwsAccountID, stack)
		params.TemplateVersion, params.TemplateSha256 = ptr(TemplateVersion), ptr(TemplateSHA256())
		node := fmt.Sprintf("lazycloud-node-%s-g%d", suffix, gen)
		params.NodeRoleArn = ptr(fmt.Sprintf("arn:aws:iam::%s:role/%s", conn.AwsAccountID, node))
		params.NodeInstanceProfile = &node
	}
	if _, err := q.InsertAuthorization(ctx, params); err != nil {
		return fmt.Errorf("insert authorization: %w", err)
	}
	return nil
}

// roleFree refuses an existing role another connection already uses.
func roleFree(ctx context.Context, q *Queries, role string, connection *uuid.UUID) error {
	if role == "" {
		return nil
	}
	taken, err := q.RoleBoundElsewhere(ctx, RoleBoundElsewhereParams{RoleArn: role, ConnectionID: connection})
	if err != nil {
		return fmt.Errorf("check role: %w", err)
	}
	if taken {
		return &ConflictError{Message: "that AWS role already backs another account's connection"}
	}
	return nil
}

// existingRoleNodeProfile names the role and instance profile an
// existing-role account provides for its instances.
const existingRoleNodeProfile = "lazycloud-node"

func connectionSuffix(conn CloudConnection) string {
	sum := sha256.Sum256([]byte(conn.AccountID.String() + "\x00" + conn.ID.String()))
	return hex.EncodeToString(sum[:])[:16]
}

func stackPrefix(suffix string) string { return "lazycloud-connection-" + suffix }

func nonNilMap(m map[string]Network) map[string]Network {
	if m == nil {
		return map[string]Network{}
	}
	return m
}

// AccountConnection returns the account's connection, or nil without one.
func (c *Compute) AccountConnection(ctx context.Context, account identity.UserID) (*Connection, error) {
	row, err := c.queries.ConnectionOfAccount(ctx, uuid.UUID(account))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read connection: %w", err)
	}
	conn, err := c.connectionByID(ctx, row.ID)
	if err != nil {
		return nil, err
	}
	return &conn, nil
}

func (c *Compute) connectionByID(ctx context.Context, id uuid.UUID) (Connection, error) {
	var conn Connection
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, err := q.LockConnection(ctx, id)
		if errors.Is(err, pgx.ErrNoRows) {
			return &NotFoundError{Message: "AWS account connection not found"}
		}
		if err != nil {
			return fmt.Errorf("read connection: %w", err)
		}
		auths, err := q.ConnectionAuthorizations(ctx, id)
		if err != nil {
			return fmt.Errorf("read authorizations: %w", err)
		}
		conn = connectionOf(row, auths)
		return nil
	})
	if err != nil {
		return Connection{}, fmt.Errorf("read connection: %w", err)
	}
	return conn, nil
}

func connectionOf(row CloudConnection, auths []CloudAuthorization) Connection {
	conn := Connection{
		ID: row.ID, AWSAccountID: row.AwsAccountID, Phase: ConnectionPhase(row.Phase), Revision: int(row.Revision),
		NextRetryAt: row.NextStepAt, CreatedAt: row.CreatedAt, UpdatedAt: row.UpdatedAt, removalRequested: row.RemovalRequestedAt,
	}
	if row.ActionUrl != nil && row.ActionLabel != nil {
		conn.Customer = &CustomerStep{URL: *row.ActionUrl, Label: *row.ActionLabel}
	}
	for _, a := range auths {
		auth := authorizationOf(a)
		switch authorizationSlot(deref(a.Slot)) {
		case slotActive:
			conn.Active = &auth
		case slotPending:
			conn.Pending = &auth
		case slotRetiring:
			conn.Retiring = &auth
		}
	}
	return conn
}

func roleName(arn string) string {
	if i := strings.LastIndex(arn, "/"); i >= 0 {
		return arn[i+1:]
	}
	return arn
}

func authorizationOf(a CloudAuthorization) Authorization {
	auth := Authorization{
		ID: a.ID, Generation: int(a.Generation), Mode: AuthorizationMode(a.Mode), Phase: AuthorizationPhase(a.Phase),
		RoleARN: a.RoleArn, Region: a.Region, StackName: deref(a.StackName), StackID: deref(a.StackID),
		TemplateVersion: deref(a.TemplateVersion), TemplateSHA256: deref(a.TemplateSha256),
		ErrorMessage: deref(a.ErrorMessage), ValidationStarted: a.LastValidationStartedAt, LastValidated: a.LastValidatedAt,
		CreatedAt: a.CreatedAt, UpdatedAt: a.UpdatedAt,
		externalID: a.ExternalID, nodeRoleARN: deref(a.NodeRoleArn), nodeInstanceProfle: deref(a.NodeInstanceProfile),
		expiresAt: a.ExpiresAt, deleteRequested: a.StackDeleteRequestedAt,
	}
	if a.ErrorCode != nil {
		code := AuthorizationError(*a.ErrorCode)
		auth.ErrorCode = &code
	}
	// The column holds what validation or Connect wrote.
	_ = json.Unmarshal(a.Networks, &auth.networks)
	return auth
}

// ExternalID is the external ID an existing-role authorization requires.
func (a Authorization) ExternalID() string { return a.externalID }

// View returns the connection as its account sees it.
func (c *Compute) View(conn Connection) Connection {
	if conn.awaitsStack() {
		conn.ConsoleURL = ptr(c.quickCreateURL(conn.AWSAccountID, *conn.Pending))
	}
	return conn
}

// quickCreateURL opens the AWS Console's quick-create page for a's stack
// from the published template, with every template parameter filled. The
// customer signs in to the account, acknowledges the IAM capability and
// creates the stack.
func (c *Compute) quickCreateURL(account string, a Authorization) string {
	q := url.Values{
		"templateURL":                   {c.templateURL()},
		"stackName":                     {a.StackName},
		"param_TargetAccountId":         {account},
		"param_PlatformPrincipalArn":    {c.fleet.PrincipalARN},
		"param_ExternalId":              {a.externalID},
		"param_ConnectionRoleName":      {a.StackName},
		"param_StackPrefix":             {strings.TrimSuffix(a.StackName, fmt.Sprintf("-g%d", a.Generation))},
		"param_BucketPrefix":            {c.fleet.BucketPrefix},
		"param_FleetName":               {c.fleet.Name},
		"param_NodeRoleName":            {roleName(a.nodeRoleARN)},
		"param_NodeInstanceProfileName": {a.nodeInstanceProfle},
	}
	return fmt.Sprintf("https://%s.console.aws.amazon.com/cloudformation/home?region=%s#/stacks/quickcreate?%s",
		a.Region, a.Region, q.Encode())
}

// templateKey is where the template's exact bytes are published. The key
// names their digest, so a published object never changes.
func templateKey() string { return "connection/" + TemplateSHA256() + ".json" }

// templateURL is the published template's public S3 URL, which
// CloudFormation reads the template from.
func (c *Compute) templateURL() string {
	return fmt.Sprintf("https://%s.s3.%s.amazonaws.com/%s", c.fleet.TemplateBucket, c.fleet.AWS.Region, templateKey())
}

// RunTemplatePublisher publishes the connection template until it
// succeeds, backing off between failed attempts up to five minutes apart,
// and returns when it does or ctx ends. Console links name the template by
// its digest, so connecting works meanwhile; only CloudFormation's fetch of
// the template waits for it.
func (c *Compute) RunTemplatePublisher(ctx context.Context, logger *slog.Logger) error {
	for delay := 5 * time.Second; ; delay = min(2*delay, 5*time.Minute) {
		err := c.PublishConnectionTemplate(ctx)
		if err == nil {
			return nil
		}
		logger.WarnContext(ctx, "the connection template is not published yet", "error", err, "retry_in", delay)
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(delay):
		}
	}
}

// PublishConnectionTemplate uploads the connection template to the
// template bucket unless an earlier start already did. Without a platform
// principal no connection can be made and nothing is published.
func (c *Compute) PublishConnectionTemplate(ctx context.Context) error {
	if c.fleet.PrincipalARN == "" {
		return nil
	}
	client := c.aws().s3(awsScope{key: string(KindPlatform)}, "")
	bucket, key := aws.String(c.fleet.TemplateBucket), aws.String(templateKey())
	_, err := client.HeadObject(ctx, &s3.HeadObjectInput{Bucket: bucket, Key: key})
	var missing *s3types.NotFound
	switch {
	case err == nil:
		return nil
	case !errors.As(err, &missing):
		return fmt.Errorf("check the published connection template: %w", err)
	}
	if _, err := client.PutObject(ctx, &s3.PutObjectInput{
		Bucket: bucket, Key: key, Body: strings.NewReader(connectionTemplate), ContentType: aws.String("application/json"),
	}); err != nil {
		return fmt.Errorf("publish the connection template: %w", err)
	}
	return nil
}

// setPhase moves the connection to phase, scheduling its background step.
func setPhase(ctx context.Context, q *Queries, id uuid.UUID, phase ConnectionPhase, next *time.Time, attempts int, action *CustomerStep) error {
	params := SetConnectionPhaseParams{ID: id, Phase: string(phase), NextStepAt: next, StepAttempts: int32(attempts)} //nolint:gosec // Attempts are bounded.
	if action != nil {
		params.ActionUrl, params.ActionLabel = &action.URL, &action.Label
	}
	if _, err := q.SetConnectionPhase(ctx, params); err != nil {
		return fmt.Errorf("set connection phase: %w", err)
	}
	return nil
}

func now() *time.Time { t := time.Now(); return &t }

// Reconnect starts a replacement authorization while the active one keeps
// serving. An existing-role connection must name the role to revalidate.
func (c *Compute) Reconnect(ctx context.Context, account identity.UserID, roleARN string) (Connection, error) {
	var id uuid.UUID
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, auths, err := lockAccountConnection(ctx, q, account)
		if err != nil {
			return err
		}
		id = row.ID
		conn := connectionOf(row, auths)
		if conn.Phase == ConnReconnecting && conn.Pending != nil {
			return nil
		}
		if conn.Phase != ConnReady && conn.Phase != ConnDegraded {
			return &ConflictError{Message: "AWS account connection cannot start authorization replacement"}
		}
		if conn.Active == nil {
			return &ConflictError{Message: "AWS account connection has no active authorization"}
		}
		if (roleARN != "") != (conn.Active.Mode == ModeExistingRole) {
			if roleARN == "" {
				return &InvalidError{Message: "existing-role reconnect must revalidate the connected role"}
			}
			return &ConflictError{Message: "AWS reconnect must preserve its authorization mode"}
		}
		if err := roleFree(ctx, q, roleARN, &row.ID); err != nil {
			return err
		}
		gen, err := q.NextGeneration(ctx, row.ID)
		if err != nil {
			return fmt.Errorf("next generation: %w", err)
		}
		req := ConnectRequest{AWSAccountID: row.AwsAccountID, RoleARN: roleARN}
		if roleARN != "" {
			req.ExternalID, req.Networks = conn.Active.externalID, conn.Active.networks
		}
		if err := c.insertAuthorization(ctx, q, row, int(gen), req); err != nil {
			return err
		}
		return setPhase(ctx, q, row.ID, ConnReconnecting, now(), 0, nil)
	})
	if err != nil {
		return Connection{}, fmt.Errorf("reconnect AWS: %w", err)
	}
	return c.connectionByID(ctx, id)
}

// CancelReconnect drops a pending replacement, which retires as a replaced
// authorization does, deleting its stack; the active authorization keeps
// serving.
func (c *Compute) CancelReconnect(ctx context.Context, account identity.UserID) (Connection, error) {
	var id uuid.UUID
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, auths, err := lockAccountConnection(ctx, q, account)
		if err != nil {
			return err
		}
		id = row.ID
		conn := connectionOf(row, auths)
		switch {
		case conn.Pending == nil && (conn.Phase == ConnReady || conn.Phase == ConnRetiring):
			return nil
		case conn.Phase != ConnReconnecting || conn.Pending == nil:
			return &ConflictError{Message: "AWS account connection has no pending replacement"}
		case conn.Active == nil:
			return &ConflictError{Message: "AWS authorization replacement state is incomplete"}
		}
		return retirePending(ctx, tx, q, conn)
	})
	if err != nil {
		return Connection{}, fmt.Errorf("cancel reconnect: %w", err)
	}
	return c.connectionByID(ctx, id)
}

// Disconnect removes the account's connection: the platform terminates its
// instances in the account, deletes every connection stack it asked the
// customer to create, then deletes the connection. It is refused while a
// workspace lives in the account; a workspace's bucket there, which holds
// its volumes and disks, goes before the workspace does. A removal under
// way, or waiting on the customer, is left as it is.
func (c *Compute) Disconnect(ctx context.Context, account identity.UserID) (*Connection, error) {
	var id uuid.UUID
	removed := false
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, auths, err := lockAccountConnection(ctx, q, account)
		var missing *NotFoundError
		if errors.As(err, &missing) {
			removed = true
			return nil
		}
		if err != nil {
			return err
		}
		id = row.ID
		conn := connectionOf(row, auths)
		if conn.removing() {
			return nil
		}
		workspaces, err := q.ConnectionWorkspaces(ctx, &row.ID)
		if err != nil {
			return fmt.Errorf("read workspaces: %w", err)
		}
		if len(workspaces) > 0 {
			return &ConflictError{Message: "delete the workspaces that live in this AWS account, with their volumes and disks, before disconnecting it: " + strings.Join(workspaces, ", ")}
		}
		removed, err = beginRemoval(ctx, tx, q, conn)
		return err
	})
	if err != nil {
		return nil, fmt.Errorf("disconnect AWS: %w", err)
	}
	if removed {
		return nil, nil
	}
	conn, err := c.connectionByID(ctx, id)
	if err != nil {
		return nil, err
	}
	return &conn, nil
}

// beginRemoval starts removing conn, in the caller's transaction holding
// its lock, and reports whether the connection went at once. A connected
// account drains its instances first; its pending and retiring
// authorizations leave their slots, and their stacks go with the active
// one's. A setup that never finished launched nothing, so its removal
// starts at its stacks, and one with no stack goes at once.
func beginRemoval(ctx context.Context, tx pgx.Tx, q *Queries, conn Connection) (bool, error) {
	phase := ConnDraining
	if conn.Active == nil {
		stacks, err := q.RemainingStacks(ctx, conn.ID)
		if err != nil {
			return false, fmt.Errorf("read stacks: %w", err)
		}
		if len(stacks) == 0 {
			if err := q.DeleteConnection(ctx, conn.ID); err != nil {
				return false, fmt.Errorf("delete connection: %w", err)
			}
			return true, nil
		}
		phase = ConnRevoking
	} else {
		for _, a := range []*Authorization{conn.Pending, conn.Retiring} {
			if a != nil {
				if err := q.SetAuthorizationPhase(ctx, SetAuthorizationPhaseParams{ID: a.ID, Phase: string(AuthRetired)}); err != nil {
					return false, fmt.Errorf("retire authorization: %w", err)
				}
			}
		}
	}
	if err := q.RequestRemoval(ctx, conn.ID); err != nil {
		return false, fmt.Errorf("record removal: %w", err)
	}
	if err := setPhase(ctx, q, conn.ID, phase, now(), 0, nil); err != nil {
		return false, err
	}
	return false, notifyChannel(ctx, tx, conn.ID)
}

// LeftoverKind is what the platform could not confirm deleted.
type LeftoverKind string

const (
	LeftoverStack    LeftoverKind = "stack"
	LeftoverInstance LeftoverKind = "instance"
)

// Leftover is a resource that may remain in the customer's AWS account.
type Leftover struct {
	Kind       LeftoverKind
	ID         string
	Region     string
	ConsoleURL string
}

// Forget stops managing an account whose removal waits on the customer. It
// removes the account's launch permission on the fleet's images, deletes the
// connection's rows and returns what may remain in AWS for the customer to
// delete: every stack and every host instance not confirmed gone.
func (c *Compute) Forget(ctx context.Context, account identity.UserID) ([]Leftover, error) {
	stuck := func(conn Connection) error {
		if conn.Phase != ConnActionRequire || conn.Retiring != nil {
			return &ConflictError{Message: "only a disconnect that needs attention in AWS can be forgotten"}
		}
		return nil
	}
	conn, err := c.AccountConnection(ctx, account)
	switch {
	case err != nil:
		return nil, err
	case conn == nil:
		return nil, &NotFoundError{Message: "AWS account connection not found"}
	}
	if err := stuck(*conn); err != nil {
		return nil, err
	}
	if err := c.unshareImages(ctx, *conn); err != nil {
		return nil, fmt.Errorf("forget AWS connection: %w", err)
	}
	left := []Leftover{}
	err = pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, auths, err := lockAccountConnection(ctx, q, account)
		if err != nil {
			return err
		}
		if err := stuck(connectionOf(row, auths)); err != nil {
			return err
		}
		stacks, err := q.RemainingStacks(ctx, row.ID)
		if err != nil {
			return fmt.Errorf("read stacks: %w", err)
		}
		for _, s := range stacks {
			left = append(left, Leftover{
				Kind: LeftoverStack, ID: cmp.Or(deref(s.StackID), s.StackName), Region: s.Region,
				ConsoleURL: stackURL(s.Region, deref(s.StackID), s.StackName),
			})
		}
		instances, err := q.LiveConnectionInstances(ctx, &row.ID)
		if err != nil {
			return fmt.Errorf("read instances: %w", err)
		}
		for _, i := range instances {
			left = append(left, Leftover{Kind: LeftoverInstance, ID: i.InstanceID, Region: i.Region, ConsoleURL: instanceURL(i.Region, i.InstanceID)})
		}
		if err := q.AbandonConnectionHosts(ctx, &row.ID); err != nil {
			return fmt.Errorf("end hosts: %w", err)
		}
		if err := q.DeleteConnection(ctx, row.ID); err != nil {
			return fmt.Errorf("delete connection: %w", err)
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("forget AWS connection: %w", err)
	}
	return left, nil
}

// Retry reruns the step the connection waits on now.
func (c *Compute) Retry(ctx context.Context, account identity.UserID) (Connection, error) {
	var id uuid.UUID
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, auths, err := lockAccountConnection(ctx, q, account)
		if err != nil {
			return err
		}
		id = row.ID
		conn := connectionOf(row, auths)
		switch conn.Phase {
		case ConnActionRequire:
			// A removal starts over from its drain, which passes at once
			// without hosts.
			next := ConnDraining
			if conn.Retiring != nil {
				next = ConnRetiring
			} else if err := q.RequestRemoval(ctx, row.ID); err != nil {
				return fmt.Errorf("record removal: %w", err)
			}
			if err := setPhase(ctx, q, row.ID, next, now(), 0, nil); err != nil {
				return err
			}
			return notifyChannel(ctx, tx, row.ID)
		case ConnDegraded, ConnAwaiting, ConnReconnecting:
			return setPhase(ctx, q, row.ID, conn.Phase, now(), 0, nil)
		case ConnValidating, ConnReady, ConnRetiring, ConnDraining, ConnRevoking, ConnVerifying:
		}
		return nil
	})
	if err != nil {
		return Connection{}, fmt.Errorf("retry connection: %w", err)
	}
	return c.connectionByID(ctx, id)
}

func lockAccountConnection(ctx context.Context, q *Queries, account identity.UserID) (CloudConnection, []CloudAuthorization, error) {
	row, err := q.LockConnectionOfAccount(ctx, uuid.UUID(account))
	if errors.Is(err, pgx.ErrNoRows) {
		return CloudConnection{}, nil, &NotFoundError{Message: "AWS account connection not found"}
	}
	if err != nil {
		return CloudConnection{}, nil, fmt.Errorf("lock connection: %w", err)
	}
	auths, err := q.ConnectionAuthorizations(ctx, row.ID)
	if err != nil {
		return CloudConnection{}, nil, fmt.Errorf("read authorizations: %w", err)
	}
	return row, auths, nil
}

// PlaceWorkspace returns the connection a new workspace of account goes
// into, inside the transaction that creates the workspace. The connection
// must accept new work. Its row stays share-locked until that transaction
// ends, so a concurrent Disconnect, which locks the row for update, either
// sees the workspace or the workspace sees the removal.
func (c *Compute) PlaceWorkspace(ctx context.Context, tx pgx.Tx, account identity.UserID) (uuid.UUID, error) {
	q := c.queries.WithTx(tx)
	row, err := q.LockWorkspaceConnection(ctx, uuid.UUID(account))
	if errors.Is(err, pgx.ErrNoRows) {
		return uuid.UUID{}, &ConflictError{Message: "no AWS account is connected; run `lazycloud cloud connect aws` first"}
	}
	if err != nil {
		return uuid.UUID{}, fmt.Errorf("lock connection: %w", err)
	}
	auths, err := q.ConnectionAuthorizations(ctx, row.ID)
	if err != nil {
		return uuid.UUID{}, fmt.Errorf("read authorizations: %w", err)
	}
	if conn := connectionOf(row, auths); !conn.HostsWorkloads() {
		return uuid.UUID{}, &ConflictError{Message: fmt.Sprintf(
			"the connected AWS account is %s; a workspace can be created there once `lazycloud cloud status` reports ready", conn.Phase)}
	}
	return row.ID, nil
}

// ConnectedAccount is a connected AWS account as storage reaches it: the
// account and the region of its active authorization, where the account's
// workspace buckets are created.
type ConnectedAccount struct {
	AWSAccountID string
	Region       string
	role         string
	externalID   string
}

// ConnectedAccount returns the account of connection. Without an active
// authorization the platform cannot act there, which is a *ConflictError.
func (c *Compute) ConnectedAccount(ctx context.Context, connection uuid.UUID) (ConnectedAccount, error) {
	row, err := c.queries.ConnectionAccess(ctx, connection)
	if errors.Is(err, pgx.ErrNoRows) {
		return ConnectedAccount{}, &ConflictError{Message: "the AWS account connection has no active authorization"}
	}
	if err != nil {
		return ConnectedAccount{}, fmt.Errorf("read connection access: %w", err)
	}
	if AuthorizationPhase(row.Phase) == AuthDegraded && deref(row.ErrorCode) == string(ErrAssumeRoleDenied) {
		return ConnectedAccount{}, &ConflictError{Message: "the connected AWS account refuses LazyCloud its role: " + deref(row.ErrorMessage)}
	}
	return ConnectedAccount{AWSAccountID: row.AwsAccountID, Region: row.Region, role: row.RoleArn, externalID: row.ExternalID}, nil
}

// S3Endpoint is where connected accounts' buckets are reached: empty for
// AWS S3 in each bucket's region.
func (c *Compute) S3Endpoint() string { return c.fleet.Endpoints.S3 }

// AssumeConnectionRole returns credentials of the account's role for
// lifetime, assumed through the platform principal with the external ID. A
// non-empty policy narrows the session to what it allows. AWS's refusal
// keeps its API error.
func (c *Compute) AssumeConnectionRole(ctx context.Context, account ConnectedAccount, session, policy string, lifetime time.Duration) (aws.Credentials, error) {
	creds, err := stscreds.NewAssumeRoleProvider(c.sts, account.role, func(o *stscreds.AssumeRoleOptions) {
		o.ExternalID, o.RoleSessionName, o.Duration = aws.String(account.externalID), session, lifetime
		if policy != "" {
			o.Policy = aws.String(policy)
		}
	}).Retrieve(ctx)
	if err != nil {
		return aws.Credentials{}, fmt.Errorf("assume the connection role %s: %w", account.role, err)
	}
	return creds, nil
}

// AdvanceConnections runs the background steps that are due: it
// revalidates waiting authorizations, retires replaced ones and removes
// disconnected accounts. Each due connection is claimed for one step, so
// scheduler replicas never run the same one at once, and each step commits
// on its own.
func (c *Compute) AdvanceConnections(ctx context.Context, logger *slog.Logger) (int, error) {
	for n := range connectionStepsPerPass {
		id, err := c.queries.ClaimDueConnection(ctx, connectionStepLease.Seconds())
		if errors.Is(err, pgx.ErrNoRows) {
			return n, nil
		}
		if err != nil {
			return n, fmt.Errorf("claim a due connection: %w", err)
		}
		step, cancel := context.WithTimeout(ctx, connectionStepDeadline)
		err = c.advanceConnection(step, id)
		cancel()
		if err != nil {
			if ctx.Err() != nil {
				return n, err
			}
			logger.ErrorContext(ctx, "advance connection", "connection_id", id, "error", err)
		}
	}
	return connectionStepsPerPass, nil
}
