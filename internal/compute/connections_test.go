package compute_test

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"sync"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

const platformPrincipal = "arn:aws:iam::999988887777:role/lazycloud-platform"

// customerAWS is what customer accounts hold, served through the emulator:
// roles the platform may assume and the connection stacks CloudFormation
// made.
type customerAWS struct {
	mu sync.Mutex
	// roles maps a role ARN to the external ID AssumeRole requires.
	roles map[string]role
	// stacks are by name.
	stacks map[string]*cfnStack
	// failDelete makes deleting the named stack end in DELETE_FAILED.
	failDelete map[string]bool
}

type role struct {
	externalID string
	// open accepts AssumeRole with any external ID or none.
	open bool
}

// assumedKey is the access key AssumeRole issues for a role; STS reads the
// account and role back from it.
func assumedKey(roleARN string) string {
	account := strings.Split(roleARN, ":")[4]
	return "ASIA-" + account + "-" + roleARN[strings.LastIndex(roleARN, "/")+1:]
}

func newCustomerAWS(e *awsEmulator) *customerAWS {
	c := &customerAWS{roles: map[string]role{}, stacks: map[string]*cfnStack{}, failDelete: map[string]bool{}}
	e.on("AssumeRole", func(call awsCall) awsReply {
		c.mu.Lock()
		defer c.mu.Unlock()
		arn, external := call.Form.Get("RoleArn"), call.Form.Get("ExternalId")
		r, ok := c.roles[arn]
		if call.AccessKey != platformAccessKey || !ok || (!r.open && (external == "" || external != r.externalID)) {
			return queryError(http.StatusForbidden, "AccessDenied",
				"User: "+platformPrincipal+" is not authorized to perform: sts:AssumeRole on resource: "+arn)
		}
		return assumeRoleReply(assumedKey(arn), arn, call.Form.Get("RoleSessionName"))
	})
	e.on("GetCallerIdentity", func(call awsCall) awsReply {
		parts := strings.SplitN(call.AccessKey, "-", 3)
		if len(parts) != 3 {
			return queryError(http.StatusForbidden, "InvalidClientTokenId", "The security token included in the request is invalid")
		}
		return callerIdentityReply(parts[1], "arn:aws:sts::"+parts[1]+":assumed-role/"+parts[2]+"/lazycloud", "AROAEXAMPLE:lazycloud")
	})
	e.on("DescribeStacks", func(call awsCall) awsReply {
		c.mu.Lock()
		defer c.mu.Unlock()
		s, ok := c.stacks[call.Form.Get("StackName")]
		if !ok || !strings.HasPrefix(call.AccessKey, "ASIA-") {
			return queryError(http.StatusBadRequest, "ValidationError", "Stack with id "+call.Form.Get("StackName")+" does not exist")
		}
		return describeStacksReply(*s)
	})
	e.on("DeleteStack", func(call awsCall) awsReply {
		c.mu.Lock()
		defer c.mu.Unlock()
		name := call.Form.Get("StackName")
		if s, ok := c.stacks[name]; ok {
			s.Status = "DELETE_COMPLETE"
			if c.failDelete[name] {
				s.Status = "DELETE_FAILED"
			}
		}
		return deleteStackReply()
	})
	e.on("DescribeSubnets", func(call awsCall) awsReply {
		zones := map[string]compute.Subnet{
			"subnet-a": {ID: "subnet-a", Zone: "us-east-2a", ZoneID: "use2-az1"},
			"subnet-b": {ID: "subnet-b", Zone: "us-east-2b", ZoneID: "use2-az2"},
		}
		var subnets []compute.Subnet
		for _, id := range list(call.Form, "SubnetId") {
			subnets = append(subnets, zones[id])
		}
		return describeSubnetsReply("vpc-customer", subnets...)
	})
	return c
}

// authorize does what the customer's CloudFormation stack does: it creates
// the connection role that demands the stack's external ID, and the stack
// with its outputs.
func (c *customerAWS) authorize(t *testing.T, o owners, conn compute.Connection, r role) {
	t.Helper()
	stack := o.compute.View(conn).Stack
	if stack == nil || conn.Pending == nil {
		t.Fatalf("connection %s waits for no stack", conn.Phase)
	}
	params := map[string]string{}
	for _, p := range stack.Parameters {
		params[p[0]] = p[1]
	}
	if r.externalID == "" && !r.open {
		r.externalID = params["ExternalId"]
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.roles[conn.Pending.RoleARN] = r
	c.stacks[stack.StackName] = &cfnStack{
		Name: stack.StackName, Status: "CREATE_COMPLETE",
		ID: "arn:aws:cloudformation:us-east-2:" + stack.AccountID + ":stack/" + stack.StackName + "/" + uuid.NewString(),
		Outputs: map[string]string{
			"ConnectionRoleArn": conn.Pending.RoleARN, "VpcId": "vpc-customer", "SecurityGroupId": "sg-customer",
			"SubnetIds": "subnet-a,subnet-b", "NodeInstanceProfileName": params["NodeInstanceProfileName"],
			"NodeRoleArn": "arn:aws:iam::" + stack.AccountID + ":role/" + params["NodeRoleName"],
		},
	}
}

func (c *customerAWS) externalID(roleARN string) string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.roles[roleARN].externalID
}

func (c *customerAWS) setStatus(name, status string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stacks[name].Status = status
}

func connectionFleet(t *testing.T) (owners, *awsEmulator, *customerAWS) {
	t.Helper()
	emulator := newAWS(t)
	o := newOwners(t, fleetConfig(emulator.fleet(compute.Fleet{PrincipalARN: platformPrincipal})))
	return o, emulator, newCustomerAWS(emulator)
}

func connect(t *testing.T, o owners, account identity.UserID, awsAccount string) compute.Connection {
	t.Helper()
	conn, err := o.compute.Connect(t.Context(), account, compute.ConnectRequest{AWSAccountID: awsAccount})
	if err != nil {
		t.Fatal(err)
	}
	return conn
}

func validate(t *testing.T, o owners, account identity.UserID) compute.Connection {
	t.Helper()
	conn, err := o.compute.Validate(t.Context(), account)
	if err != nil {
		t.Fatalf("validate: %v", err)
	}
	return conn
}

// connected brings account's managed connection to AWS account
// 111111111111 to ready.
func connected(t *testing.T, o owners, customer *customerAWS, account identity.UserID) compute.Connection {
	t.Helper()
	customer.authorize(t, o, connect(t, o, account, "111111111111"), role{})
	conn := validate(t, o, account)
	if conn.Phase != compute.ConnReady {
		t.Fatalf("connection %s (%+v), want ready", conn.Phase, conn.Pending)
	}
	return conn
}

func advance(t *testing.T, o owners) {
	t.Helper()
	if _, err := o.compute.AdvanceConnections(t.Context(), discard()); err != nil {
		t.Fatal(err)
	}
}

// due makes the connection's waiting step due now.
func due(t *testing.T, o owners, conn compute.Connection) {
	t.Helper()
	run(t, o.pool, "update cloud_connections set next_step_at = now() where id = $1", conn.ID)
}

func current(t *testing.T, o owners, account identity.UserID) *compute.Connection {
	t.Helper()
	conn, err := o.compute.AccountConnection(t.Context(), account)
	if err != nil {
		t.Fatal(err)
	}
	return conn
}

func TestConnectingAWSNeedsThePlanThatIncludesIt(t *testing.T) {
	o, _, _ := connectionFleet(t)
	free := identity.UserID(scan[uuid.UUID](t, o.pool, "insert into users (email) values ('free@example.com') returning id"))
	var unpaid *billing.PaymentRequiredError
	if _, err := o.compute.Connect(t.Context(), free, compute.ConnectRequest{AWSAccountID: "111111111111"}); !errors.As(err, &unpaid) {
		t.Fatalf("a Free account connected AWS: %v", err)
	}
	if n := scan[int](t, o.pool, "select count(*)::int from cloud_connections"); n != 0 {
		t.Fatalf("%d connections recorded", n)
	}
}

func TestConnectChecksItsRequestAndRepeatsAnUnfinishedSetup(t *testing.T) {
	ctx := t.Context()
	o, _, _ := connectionFleet(t)
	alice, bob := newUser(t, o.pool, "alice@example.com"), newUser(t, o.pool, "bob@example.com")

	var invalid *compute.InvalidError
	for name, req := range map[string]compute.ConnectRequest{
		"a role in another account":     {AWSAccountID: "111111111111", RoleARN: "arn:aws:iam::222222222222:role/lazycloud"},
		"networks without a role":       {AWSAccountID: "111111111111", Networks: fleetNetworks()},
		"an external ID without a role": {AWSAccountID: "111111111111", ExternalID: strings.Repeat("x", 40)},
	} {
		if _, err := o.compute.Connect(ctx, alice, req); !errors.As(err, &invalid) {
			t.Errorf("%s: %v, want InvalidError", name, err)
		}
	}
	var unavailable *compute.UnavailableError
	unconfigured := compute.NewCompute(o.pool, o.execution, compute.Config{})
	if _, err := unconfigured.Connect(ctx, alice, compute.ConnectRequest{AWSAccountID: "111111111111"}); !errors.As(err, &unavailable) {
		t.Errorf("connect without a platform principal: %v, want UnavailableError", err)
	}

	conn := connect(t, o, alice, "111111111111")
	stack := o.compute.View(conn).Stack
	if conn.Phase != compute.ConnAwaiting || stack == nil || stack.TemplateSHA256 != compute.TemplateSHA256() {
		t.Fatalf("connection %s with stack %+v, want awaiting authorization with the current template", conn.Phase, stack)
	}
	params := map[string]string{}
	for _, p := range stack.Parameters {
		params[p[0]] = p[1]
	}
	if len(params["ExternalId"]) < 32 || params["PlatformPrincipalArn"] != platformPrincipal ||
		params["FleetName"] != "lazycloud-test" || params["TargetAccountId"] != "111111111111" {
		t.Fatalf("stack parameters %v", params)
	}
	again := connect(t, o, alice, "111111111111")
	if again.ID != conn.ID || again.Pending.ID != conn.Pending.ID {
		t.Fatalf("repeat connect made %s/%s, want the unfinished setup %s/%s", again.ID, again.Pending.ID, conn.ID, conn.Pending.ID)
	}
	var conflict *compute.ConflictError
	if _, err := o.compute.Connect(ctx, alice, compute.ConnectRequest{AWSAccountID: "333333333333"}); !errors.As(err, &conflict) {
		t.Fatalf("connect a second AWS account: %v, want ConflictError", err)
	}

	existing, err := o.compute.Connect(ctx, bob, compute.ConnectRequest{
		AWSAccountID: "222222222222", RoleARN: "arn:aws:iam::222222222222:role/lazycloud", Networks: fleetNetworks(),
	})
	if err != nil {
		t.Fatal(err)
	}
	if existing.Pending == nil || existing.Pending.Mode != compute.ModeExistingRole || len(existing.Pending.ExternalID()) < 32 || existing.Stack != nil {
		t.Fatalf("existing-role connection %+v, want a pending role with a generated external ID and no stack", existing.Pending)
	}
}

func TestValidationProvesTheRoleAndReadsTheStackNetwork(t *testing.T) {
	o, emulator, customer := connectionFleet(t)
	ready, denied, open, drifted := newUser(t, o.pool, "ready@example.com"), newUser(t, o.pool, "denied@example.com"),
		newUser(t, o.pool, "open@example.com"), newUser(t, o.pool, "drifted@example.com")

	conn := connected(t, o, customer, ready)
	if conn.Active == nil || conn.Active.Phase != compute.AuthReady || conn.Pending != nil || !conn.HostsWorkloads() {
		t.Fatalf("validated connection %+v", conn)
	}
	var raw []byte
	if err := o.pool.QueryRow(t.Context(), "select networks from cloud_authorizations where id = $1", conn.Active.ID).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var networks map[string]compute.Network
	if err := json.Unmarshal(raw, &networks); err != nil {
		t.Fatal(err)
	}
	east := networks["us-east-2"]
	if east.VPCID != "vpc-customer" || east.SecurityGroupID != "sg-customer" || len(east.Subnets) != 2 ||
		east.Subnets[0].Zone != "us-east-2a" || east.Subnets[1].ZoneID != "use2-az2" {
		t.Fatalf("recorded networks %+v, want the stack's VPC and subnets with their zones", networks)
	}
	// The role was proven to refuse AssumeRole without the exact external ID.
	var probes int
	for _, c := range emulator.calls("AssumeRole") {
		if c.Form.Get("RoleArn") == conn.Active.RoleARN && c.Form.Get("ExternalId") != customer.externalID(conn.Active.RoleARN) {
			probes++
		}
	}
	if probes < 2 {
		t.Fatalf("%d AssumeRole probes without the external ID, want one without it and one with a wrong one", probes)
	}

	connect(t, o, denied, "222222222222")
	if conn := validate(t, o, denied); conn.Phase != compute.ConnAwaiting || code(conn.Pending) != compute.ErrAssumeRoleDenied {
		t.Fatalf("role not created yet: %s %v, want awaiting_authorization with assume_role_denied", conn.Phase, code(conn.Pending))
	}
	customer.authorize(t, o, connect(t, o, open, "333333333333"), role{open: true})
	if conn := validate(t, o, open); conn.Phase == compute.ConnReady || code(conn.Pending) != compute.ErrExternalIDOpen {
		t.Fatalf("role open to any external ID: %s %v, want external_id_not_enforced", conn.Phase, code(conn.Pending))
	}
	pending := connect(t, o, drifted, "444444444444")
	customer.authorize(t, o, pending, role{})
	customer.setStatus(pending.Pending.StackName, "CREATE_IN_PROGRESS")
	if conn := validate(t, o, drifted); conn.Phase == compute.ConnReady || code(conn.Pending) != compute.ErrStackDrift {
		t.Fatalf("stack still creating: %s %v, want stack_drift", conn.Phase, code(conn.Pending))
	}
}

func code(a *compute.Authorization) compute.AuthorizationError {
	if a == nil || a.ErrorCode == nil {
		return ""
	}
	return *a.ErrorCode
}

func TestReconnectRetiresTheReplacedStack(t *testing.T) {
	ctx := t.Context()
	o, emulator, customer := connectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	first := connected(t, o, customer, alice)

	conn, err := o.compute.Reconnect(ctx, alice, "")
	if err != nil {
		t.Fatal(err)
	}
	if conn.Phase != compute.ConnReconnecting || conn.Pending == nil || conn.Pending.Generation != 2 || !conn.HostsWorkloads() {
		t.Fatalf("reconnect: %s pending %+v, want a second generation while the first keeps serving", conn.Phase, conn.Pending)
	}
	customer.authorize(t, o, conn, role{})
	conn = validate(t, o, alice)
	if conn.Phase != compute.ConnRetiring || conn.Active == nil || conn.Active.Generation != 2 || conn.Retiring == nil {
		t.Fatalf("after validating the replacement: %s active %+v retiring %+v", conn.Phase, conn.Active, conn.Retiring)
	}
	// A host launched under the first generation runs as its role, so the
	// stack stays until the host drained and is gone.
	host := newHost(t, o.pool, hostSpec{Kind: compute.KindConnection, Provider: compute.ProviderAWS, Region: "us-east-2", InstanceID: "i-0000000000000f001"})
	run(t, o.pool, "update hosts set connection_id = $1, authorization_id = $2 where id = $3", conn.ID, first.Active.ID, uuid.UUID(host))
	advance(t, o)
	if phase, _ := hostPhase(t, o.pool, host); phase != string(compute.PhaseDraining) || len(emulator.calls("DeleteStack")) != 0 {
		t.Fatalf("with a host on the old role: host %s, %d DeleteStack calls; want it draining and the stack kept", phase, len(emulator.calls("DeleteStack")))
	}
	run(t, o.pool, "update hosts set phase = 'deleted' where id = $1", uuid.UUID(host))
	due(t, o, conn)
	advance(t, o)
	deletes := emulator.calls("DeleteStack")
	if len(deletes) != 1 || deletes[0].Form.Get("StackName") != first.Active.StackName ||
		deletes[0].AccessKey != assumedKey(conn.Active.RoleARN) {
		t.Fatalf("DeleteStack calls %v, want the first generation's stack deleted through the new role", deletes)
	}
	due(t, o, conn)
	advance(t, o)
	if conn := current(t, o, alice); conn == nil || conn.Phase != compute.ConnReady || conn.Retiring != nil || conn.Active.Generation != 2 {
		t.Fatalf("after the old stack is gone: %+v, want ready on the second generation", conn)
	}
}

func TestDisconnectDrainsInstancesThenRemovesTheStack(t *testing.T) {
	ctx := t.Context()
	o, emulator, customer := connectionFleet(t)
	alice, bob := newUser(t, o.pool, "alice@example.com"), newUser(t, o.pool, "bob@example.com")
	conn := connected(t, o, customer, alice)
	ws := newWorkspace(t, o.pool, "prod", alice)
	run(t, o.pool, "update workspaces set connection_id = $1 where id = $2", conn.ID, ws)
	var conflict *compute.ConflictError
	if _, err := o.compute.Disconnect(ctx, alice); !errors.As(err, &conflict) {
		t.Fatalf("disconnect with a workspace in the account: %v, want ConflictError", err)
	}
	run(t, o.pool, "update workspaces set connection_id = null where id = $1", ws)

	// An unfinished setup goes at once.
	connect(t, o, bob, "222222222222")
	if gone, err := o.compute.Disconnect(ctx, bob); err != nil || gone != nil || current(t, o, bob) != nil {
		t.Fatalf("disconnect an unfinished setup: %+v %v, want it removed", gone, err)
	}

	host := newHost(t, o.pool, hostSpec{Kind: compute.KindConnection, Provider: compute.ProviderAWS, Market: compute.MarketSpot,
		Region: "us-east-2", InstanceID: "i-0000000000000c001"})
	run(t, o.pool, "update hosts set connection_id = $1 where id = $2", conn.ID, uuid.UUID(host))
	emulator.on("TerminateInstances", terminateInstancesReply)
	emulator.on("DescribeInstances", func(awsCall) awsReply { return describeInstancesReply() })

	draining, err := o.compute.Disconnect(ctx, alice)
	if err != nil || draining == nil || draining.Phase != compute.ConnDraining {
		t.Fatalf("disconnect: %+v %v, want disconnect_draining", draining, err)
	}
	advance(t, o)
	if conn := current(t, o, alice); conn.Phase != compute.ConnDraining {
		t.Fatalf("with an instance left the connection is %s, want still draining", conn.Phase)
	}
	if _, err := o.compute.Retire(ctx, discard()); err != nil {
		t.Fatal(err)
	}
	terms := emulator.calls("TerminateInstances")
	if len(terms) != 1 || terms[0].AccessKey != assumedKey(conn.Active.RoleARN) {
		t.Fatalf("TerminateInstances calls %v, want the account's instance terminated through its role", terms)
	}
	if err := o.compute.Reconcile(ctx, discard()); err != nil {
		t.Fatal(err)
	}
	if phase, _ := hostPhase(t, o.pool, host); phase != string(compute.PhaseDeleted) {
		t.Fatalf("terminated connection host is %s, want deleted", phase)
	}
	due(t, o, conn)
	advance(t, o)
	if conn := current(t, o, alice); conn.Phase != compute.ConnRevoking {
		t.Fatalf("without instances the connection is %s, want revoking", conn.Phase)
	}
	advance(t, o)
	if conn := current(t, o, alice); conn.Phase != compute.ConnVerifying {
		t.Fatalf("after DeleteStack the connection is %s, want verifying_revocation", conn.Phase)
	}
	if deletes := emulator.calls("DeleteStack"); len(deletes) != 1 || deletes[0].Form.Get("StackName") != conn.Active.StackName {
		t.Fatalf("DeleteStack calls %v, want the active stack", deletes)
	}
	due(t, o, conn)
	advance(t, o)
	if conn := current(t, o, alice); conn != nil {
		t.Fatalf("after the stack is gone the connection is %s, want deleted", conn.Phase)
	}
}

func TestFailedStackDeletionAsksTheCustomerAndRetryResumes(t *testing.T) {
	ctx := t.Context()
	o, _, customer := connectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	conn := connected(t, o, customer, alice)
	customer.mu.Lock()
	customer.failDelete[conn.Active.StackName] = true
	customer.mu.Unlock()

	if _, err := o.compute.Disconnect(ctx, alice); err != nil {
		t.Fatal(err)
	}
	advance(t, o) // drained: nothing runs there
	advance(t, o) // DeleteStack
	due(t, o, conn)
	advance(t, o)
	stuck := current(t, o, alice)
	if stuck == nil || stuck.Phase != compute.ConnActionRequire ||
		!strings.Contains(stuck.ActionURL, "console.aws.amazon.com/cloudformation") || !strings.Contains(stuck.ActionURL, "stackId=") {
		t.Fatalf("after DELETE_FAILED: %+v, want action_required with the stack's console URL", stuck)
	}
	if actions := stuck.Actions(); len(actions) != 1 || actions[0] != compute.ActionRetry {
		t.Fatalf("actions %v, want retry", actions)
	}

	// The customer deletes the stack in the console and retries.
	customer.setStatus(conn.Active.StackName, "DELETE_COMPLETE")
	retried, err := o.compute.Retry(ctx, alice)
	if err != nil || retried.Phase != compute.ConnRevoking {
		t.Fatalf("retry: %+v %v, want revoking", retried, err)
	}
	advance(t, o)
	if conn := current(t, o, alice); conn != nil {
		t.Fatalf("after retry the connection is %s, want deleted", conn.Phase)
	}
}
