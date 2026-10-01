package compute

import (
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
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

//go:embed connection_template.json
var connectionTemplate string

// TemplateVersion names the connection template; validation refuses stacks
// made from another one.
const TemplateVersion = "2026-09-30.v1"

// connectionRegion is where connection stacks are created.
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
}

// StackAction is the CreateStack request the customer submits.
type StackAction struct {
	AccountID      string
	Region         string
	TemplateSHA256 string
	StackName      string
	TemplateBody   string
	Parameters     [][2]string
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
	ActionURL    string
	ActionLabel  string
	CreatedAt    time.Time
	UpdatedAt    time.Time
	// Stack is the CreateStack request while a managed authorization waits
	// for the customer.
	Stack *StackAction
}

// HostsWorkloads reports whether new workloads can be placed in the account.
func (c Connection) HostsWorkloads() bool {
	if c.Active == nil || c.Active.Phase != AuthReady {
		return false
	}
	switch c.Phase {
	case ConnReady, ConnReconnecting, ConnRetiring:
		return true
	case ConnActionRequire:
		return c.Retiring != nil
	case ConnAwaiting, ConnValidating, ConnDegraded, ConnDraining, ConnRevoking, ConnVerifying:
	}
	return false
}

// removing reports whether the connection is being disconnected.
func (c Connection) removing() bool {
	return c.Phase == ConnDraining || c.Phase == ConnRevoking || c.Phase == ConnVerifying
}

// ManagesCapacity reports whether the platform still runs the account's
// existing instances.
func (c Connection) ManagesCapacity() bool {
	return c.HostsWorkloads() || (c.Phase == ConnDraining && c.Active != nil && c.Active.Phase == AuthReady)
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
		if c.Active != nil {
			return []ConnectionAction{ActionReconnect, ActionValidate, ActionRemove}
		}
		return []ConnectionAction{ActionValidate, ActionRemove}
	case ConnReconnecting:
		return []ConnectionAction{ActionAuthorize, ActionValidate, ActionCancelReconnect, ActionRemove}
	case ConnActionRequire:
		if c.Retiring != nil {
			return []ConnectionAction{ActionRetry, ActionRemove}
		}
		return []ConnectionAction{ActionRetry}
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
		NextRetryAt: row.NextStepAt, ActionURL: deref(row.ActionUrl), ActionLabel: deref(row.ActionLabel),
		CreatedAt: row.CreatedAt, UpdatedAt: row.UpdatedAt,
	}
	for _, a := range auths {
		auth := authorizationOf(a)
		switch deref(a.Slot) {
		case "active":
			conn.Active = &auth
		case "pending":
			conn.Pending = &auth
		case "retiring":
			conn.Retiring = &auth
		}
	}
	waiting := conn.Phase == ConnAwaiting || conn.Phase == ConnReconnecting || conn.Phase == ConnDegraded
	if p := conn.Pending; p != nil && p.Mode == ModeManagedStack && waiting {
		conn.Stack = &StackAction{
			AccountID: conn.AWSAccountID, Region: p.Region, TemplateSHA256: p.TemplateSHA256,
			StackName: p.StackName, TemplateBody: connectionTemplate,
			Parameters: [][2]string{
				{"ConnectionRoleName", p.StackName},
				{"ExternalId", p.externalID},
				{"FleetName", "lazycloud"},
				{"NodeInstanceProfileName", p.nodeInstanceProfle},
				{"NodeRoleName", roleName(p.nodeRoleARN)},
				{"PlatformPrincipalArn", ""},
				{"StackPrefix", strings.TrimSuffix(p.StackName, fmt.Sprintf("-g%d", p.Generation))},
				{"TargetAccountId", conn.AWSAccountID},
			},
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
		expiresAt: a.ExpiresAt,
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

// fillPrincipal sets the platform principal in a stack action's parameters.
func (c *Compute) fillPrincipal(conn *Connection) {
	if conn.Stack == nil {
		return
	}
	for n, p := range conn.Stack.Parameters {
		switch p[0] {
		case "PlatformPrincipalArn":
			conn.Stack.Parameters[n][1] = c.fleet.PrincipalARN
		case "FleetName":
			conn.Stack.Parameters[n][1] = c.fleet.Name
		}
	}
}

// View returns the connection as its account sees it.
func (c *Compute) View(conn Connection) Connection {
	c.fillPrincipal(&conn)
	return conn
}

// setPhase moves the connection to phase, scheduling its background step.
func setPhase(ctx context.Context, q *Queries, id uuid.UUID, phase ConnectionPhase, next *time.Time, attempts int, action [2]string) error {
	params := SetConnectionPhaseParams{ID: id, Phase: string(phase), NextStepAt: next, StepAttempts: int32(attempts)} //nolint:gosec // Attempts are bounded.
	if action[0] != "" || action[1] != "" {
		params.ActionUrl, params.ActionLabel = ptr(action[0]), ptr(action[1])
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
		return setPhase(ctx, q, row.ID, ConnReconnecting, now(), 0, [2]string{})
	})
	if err != nil {
		return Connection{}, fmt.Errorf("reconnect AWS: %w", err)
	}
	return c.connectionByID(ctx, id)
}

// CancelReconnect drops a pending replacement; the active authorization
// keeps serving.
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
		case conn.Phase == ConnReady:
			return nil
		case conn.Phase != ConnReconnecting || conn.Pending == nil:
			return &ConflictError{Message: "AWS account connection has no pending replacement"}
		case conn.Active == nil:
			return &ConflictError{Message: "AWS authorization replacement state is incomplete"}
		}
		if err := q.SetAuthorizationPhase(ctx, SetAuthorizationPhaseParams{ID: conn.Pending.ID, Phase: string(AuthRetired)}); err != nil {
			return fmt.Errorf("retire pending authorization: %w", err)
		}
		return setPhase(ctx, q, row.ID, ConnReady, nil, 0, [2]string{})
	})
	if err != nil {
		return Connection{}, fmt.Errorf("cancel reconnect: %w", err)
	}
	return c.connectionByID(ctx, id)
}

// Disconnect removes the account's connection. An unfinished setup goes at
// once; a connected account first drains its instances, then the platform
// deletes the stack and verifies the role is gone. It is refused while a
// workspace lives in the account.
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
			return &ConflictError{Message: "delete the workspaces that live in this AWS account before disconnecting it: " + strings.Join(workspaces, ", ")}
		}
		if conn.Active == nil {
			removed = true
			if err := q.DeleteConnection(ctx, row.ID); err != nil {
				return fmt.Errorf("delete connection: %w", err)
			}
			return nil
		}
		for _, a := range []*Authorization{conn.Pending, conn.Retiring} {
			if a != nil {
				if err := q.SetAuthorizationPhase(ctx, SetAuthorizationPhaseParams{ID: a.ID, Phase: string(AuthRetired)}); err != nil {
					return fmt.Errorf("retire authorization: %w", err)
				}
			}
		}
		if err := setPhase(ctx, q, row.ID, ConnDraining, now(), 0, [2]string{}); err != nil {
			return err
		}
		return notifyCompute(ctx, tx, row.ID)
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
			next := ConnRevoking
			if conn.Retiring != nil {
				next = ConnRetiring
			}
			return setPhase(ctx, q, row.ID, next, now(), 0, [2]string{})
		case ConnDegraded, ConnAwaiting, ConnReconnecting:
			return setPhase(ctx, q, row.ID, conn.Phase, now(), 0, [2]string{})
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

// WorkspaceConnection returns the connection a new workspace of account is
// created in. It must be ready.
func (c *Compute) WorkspaceConnection(ctx context.Context, account identity.UserID) (uuid.UUID, error) {
	conn, err := c.AccountConnection(ctx, account)
	if err != nil {
		return uuid.UUID{}, err
	}
	if conn == nil {
		return uuid.UUID{}, &ConflictError{Message: "connect an AWS account with `lazycloud cloud connect aws` before creating a workspace there"}
	}
	if !conn.HostsWorkloads() {
		return uuid.UUID{}, &ConflictError{Message: fmt.Sprintf(
			"connected AWS account %s is %s; a workspace can only be created there once it is ready", conn.AWSAccountID, conn.Phase)}
	}
	return conn.ID, nil
}

func notifyCompute(ctx context.Context, tx pgx.Tx, id uuid.UUID) error {
	return notifyChannel(ctx, tx, id)
}

// ConnectionLoop advances connections whose background step is due: it
// revalidates waiting authorizations, retires replaced ones and removes
// disconnected accounts. Each connection commits on its own.
func (c *Compute) AdvanceConnections(ctx context.Context, logger *slog.Logger) (int, error) {
	due, err := c.queries.DueConnections(ctx, 50)
	if err != nil {
		return 0, fmt.Errorf("list due connections: %w", err)
	}
	for _, id := range due {
		if err := c.advanceConnection(ctx, id); err != nil {
			if ctx.Err() != nil {
				return 0, err
			}
			logger.ErrorContext(ctx, "advance connection", "connection_id", id, "error", err)
		}
	}
	return len(due), nil
}
