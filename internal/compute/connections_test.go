package compute_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

const platformPrincipal = "arn:aws:iam::999988887777:role/lazycloud-platform"

// customerAWS is what customer accounts hold, served through the emulator:
// roles the platform may assume and the connection stacks CloudFormation
// made. STS answers AssumeRole on a role that does not exist, or with the
// wrong external ID, with AccessDenied, and CloudFormation answers
// DescribeStacks on a stack name it has no live stack of with
// ValidationError, as their API references record. Deleting a stack
// removes the role it made, which the connection template deletes last.
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
	// CloudFormation reads a deleted stack by its id, as DELETE_COMPLETE,
	// but not by its name.
	e.on("DescribeStacks", func(call awsCall) awsReply {
		c.mu.Lock()
		defer c.mu.Unlock()
		named := call.Form.Get("StackName")
		s, ok := c.stacks[named]
		for _, byID := range c.stacks {
			if byID.ID == named {
				s, ok = byID, true
			}
		}
		if !ok || !strings.HasPrefix(call.AccessKey, "ASIA-") || s.Name == named && s.Status == "DELETE_COMPLETE" {
			return queryError(http.StatusBadRequest, "ValidationError", "Stack with id "+named+" does not exist")
		}
		return describeStacksReply(*s)
	})
	e.on("DescribeStackEvents", func(call awsCall) awsReply {
		c.mu.Lock()
		defer c.mu.Unlock()
		for _, s := range c.stacks {
			if s.ID == call.Form.Get("StackName") {
				return describeStackEventsReply(*s)
			}
		}
		return queryError(http.StatusBadRequest, "ValidationError", "Stack ["+call.Form.Get("StackName")+"] does not exist")
	})
	e.on("DeleteStack", func(call awsCall) awsReply {
		c.mu.Lock()
		defer c.mu.Unlock()
		name := call.Form.Get("StackName")
		if s, ok := c.stacks[name]; ok {
			s.Status = "DELETE_COMPLETE"
			if c.failDelete[name] {
				s.Status = "DELETE_FAILED"
			} else {
				delete(c.roles, s.Outputs["ConnectionRoleArn"])
			}
		}
		return deleteStackReply()
	})
	// The account runs no fleet instances unless a test says so.
	e.on("DescribeInstances", func(awsCall) awsReply { return describeInstancesReply() })
	return c
}

// quickCreate is the stack a console quick-create link opens: its name,
// the template URL and the template parameters.
type quickCreate struct {
	stackName, templateURL string
	params                 map[string]string
}

func quickCreateOf(t *testing.T, consoleURL string) quickCreate {
	t.Helper()
	_, fragment, ok := strings.Cut(consoleURL, "#/stacks/quickcreate?")
	if !ok || !strings.HasPrefix(consoleURL, "https://us-east-2.console.aws.amazon.com/cloudformation/home?region=us-east-2#") {
		t.Fatalf("console URL %q is not a us-east-2 CloudFormation quick-create link", consoleURL)
	}
	query, err := url.ParseQuery(fragment)
	if err != nil {
		t.Fatal(err)
	}
	q := quickCreate{stackName: query.Get("stackName"), templateURL: query.Get("templateURL"), params: map[string]string{}}
	for key := range query {
		if name, ok := strings.CutPrefix(key, "param_"); ok {
			q.params[name] = query.Get(key)
		}
	}
	return q
}

// authorize does what creating the stack from the console link does: it
// creates the connection role that demands the link's external ID, and
// the stack with its outputs.
func (c *customerAWS) authorize(t *testing.T, o owners, conn compute.Connection, r role) {
	t.Helper()
	view := o.compute.View(conn)
	if view.ConsoleURL == nil || conn.Pending == nil {
		t.Fatalf("connection %s waits for no stack", conn.Phase)
	}
	stack := quickCreateOf(t, *view.ConsoleURL)
	account := stack.params["TargetAccountId"]
	if r.externalID == "" && !r.open {
		r.externalID = stack.params["ExternalId"]
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.roles["arn:aws:iam::"+account+":role/"+stack.params["ConnectionRoleName"]] = r
	c.stacks[stack.stackName] = &cfnStack{
		Name: stack.stackName, Status: "CREATE_COMPLETE",
		ID: "arn:aws:cloudformation:us-east-2:" + account + ":stack/" + stack.stackName + "/" + uuid.NewString(),
		Outputs: map[string]string{
			"ConnectionRoleArn":       "arn:aws:iam::" + account + ":role/" + stack.params["ConnectionRoleName"],
			"NodeInstanceProfileName": stack.params["NodeInstanceProfileName"],
			"NodeRoleArn":             "arn:aws:iam::" + account + ":role/" + stack.params["NodeRoleName"],
		},
	}
}

func (c *customerAWS) externalID(roleARN string) string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.roles[roleARN].externalID
}

// deleteStack is the customer deleting a stack in the console, with the
// role it made.
func (c *customerAWS) deleteStack(name string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stacks[name].Status = "DELETE_COMPLETE"
	delete(c.roles, c.stacks[name].Outputs["ConnectionRoleArn"])
}

// deleteRole is the customer deleting the connection role in IAM.
func (c *customerAWS) deleteRole(arn string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.roles, arn)
}

func (c *customerAWS) setStatus(name, status string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stacks[name].Status = status
}

func connectionFleet(t *testing.T) (owners, *awsEmulator, *customerAWS) {
	t.Helper()
	emulator := newAWS(t)
	o := newOwners(t, fleetConfig(emulator.fleet(compute.Fleet{PrincipalARN: platformPrincipal, BucketPrefix: "lazycloud-ws-test"})))
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
	setNetworks(t, o, conn.ID.String(), customerNetworks())
	return conn
}

// customerNetworks are a connected account's networks as the network
// owner records them.
func customerNetworks() map[string]compute.Network {
	return map[string]compute.Network{
		"us-east-2": {VPCID: "vpc-customer", SecurityGroupID: "sg-customer", Subnets: []compute.Subnet{
			{ID: "subnet-a", Zone: "us-east-2a", ZoneID: "use2-az1"},
			{ID: "subnet-b", Zone: "us-east-2b", ZoneID: "use2-az2"},
			{ID: "subnet-c", Zone: "us-east-2c", ZoneID: "use2-az3"},
		}},
	}
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
	t.Parallel()
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

// The connection role reaches storage only in buckets named for the
// deployment and the customer's own account, and the objects in them, and
// only while that account owns the bucket.
func TestConnectionRoleStorageIsScopedToItsOwnBuckets(t *testing.T) {
	t.Parallel()
	var template struct {
		Resources map[string]struct {
			Properties struct {
				Policies []struct {
					PolicyDocument struct {
						Statement []struct {
							Action    any
							Resource  any
							Condition struct {
								StringEquals map[string]any
							}
						}
					}
				}
			}
		}
	}
	if err := json.Unmarshal(connectionTemplate(t), &template); err != nil {
		t.Fatal(err)
	}
	buckets := "arn:${AWS::Partition}:s3:::${BucketPrefix}-${AWS::AccountId}-*"
	scoped := map[string]bool{}
	for name, resource := range template.Resources {
		for _, policy := range resource.Properties.Policies {
			for _, statement := range policy.PolicyDocument.Statement {
				actions, _ := statement.Action.([]any)
				if action, ok := statement.Action.(string); ok {
					actions = []any{action}
				}
				for _, action := range actions {
					if !strings.HasPrefix(action.(string), "s3:") { //nolint:forcetypeassert // IAM actions are strings.
						continue
					}
					sub, _ := statement.Resource.(map[string]any)
					arn, _ := sub["Fn::Sub"].(string)
					if arn != buckets && arn != buckets+"/*" {
						t.Errorf("%s grants %s on %v", name, action, statement.Resource)
					}
					if owner, _ := statement.Condition.StringEquals["s3:ResourceAccount"].(map[string]any); owner["Ref"] != "AWS::AccountId" {
						t.Errorf("%s grants %s on buckets of account %q, want the stack's own", name, action, owner)
					}
					scoped[arn] = true
				}
			}
		}
	}
	if !scoped[buckets] || !scoped[buckets+"/*"] {
		t.Fatalf("the template grants storage on %v, want the account's buckets and their objects", scoped)
	}
}

func connectionTemplate(t *testing.T) []byte {
	t.Helper()
	body, err := os.ReadFile("connection_template.json")
	if err != nil {
		t.Fatal(err)
	}
	return body
}

// The console link opens the stack from the template the server
// published, with a value for every template parameter that the template
// accepts.
func TestConnectLinksTheConsoleToThePublishedTemplate(t *testing.T) {
	t.Parallel()
	store := storagetest.Config(t)
	o := newOwners(t, fleetConfig(compute.Fleet{
		PrincipalARN: platformPrincipal, BucketPrefix: "lazycloud-ws-test", TemplateBucket: store.Bucket,
		AWS:       aws.Config{Region: store.Region, Credentials: credentials.NewStaticCredentialsProvider(store.AccessKeyID, store.SecretAccessKey, "")},
		Endpoints: compute.Endpoints{S3: store.Endpoint},
	}))
	if err := o.compute.PublishConnectionTemplate(t.Context()); err != nil {
		t.Fatal(err)
	}
	conn := connect(t, o, newUser(t, o.pool, "alice@example.com"), "111111111111")
	stack := quickCreateOf(t, *o.compute.View(conn).ConsoleURL)

	key := "connection/" + compute.TemplateSHA256() + ".json"
	if want := "https://" + store.Bucket + ".s3." + store.Region + ".amazonaws.com/" + key; stack.templateURL != want {
		t.Fatalf("templateURL %s, want %s", stack.templateURL, want)
	}
	out, err := storagetest.Client().GetObject(t.Context(), &s3.GetObjectInput{Bucket: &store.Bucket, Key: &key})
	if err != nil {
		t.Fatalf("read the published template: %v", err)
	}
	defer func() { _ = out.Body.Close() }()
	published, err := io.ReadAll(out.Body)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(published, connectionTemplate(t)) {
		t.Fatal("the published template differs from the connection template")
	}

	var template struct {
		Parameters map[string]struct {
			AllowedPattern string
			MinLength      int
		}
	}
	if err := json.Unmarshal(connectionTemplate(t), &template); err != nil {
		t.Fatal(err)
	}
	for name, p := range template.Parameters {
		value, ok := stack.params[name]
		if !ok || len(value) < p.MinLength || (p.AllowedPattern != "" && !regexp.MustCompile(p.AllowedPattern).MatchString(value)) {
			t.Errorf("parameter %s = %q (given %t), want a value the template accepts", name, value, ok)
		}
	}
	for name := range stack.params {
		if _, ok := template.Parameters[name]; !ok {
			t.Errorf("the link sets %s, which the template does not declare", name)
		}
	}
	if stack.stackName != conn.Pending.StackName || stack.params["ConnectionRoleName"] != conn.Pending.StackName {
		t.Errorf("stack %s with role %s, want both named %s", stack.stackName, stack.params["ConnectionRoleName"], conn.Pending.StackName)
	}
}

func TestConnectChecksItsRequestAndRepeatsAnUnfinishedSetup(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o, _, _ := connectionFleet(t)
	alice, bob := newUser(t, o.pool, "alice@example.com"), newUser(t, o.pool, "bob@example.com")

	var invalid *compute.InvalidError
	for name, req := range map[string]compute.ConnectRequest{
		"a role in another account":     {AWSAccountID: "111111111111", RoleARN: "arn:aws:iam::222222222222:role/lazycloud"},
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
	if conn.Phase != compute.ConnAwaiting || conn.Pending == nil || conn.Pending.TemplateSHA256 != compute.TemplateSHA256() {
		t.Fatalf("connection %s pending %+v, want awaiting authorization with the current template", conn.Phase, conn.Pending)
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
		AWSAccountID: "222222222222", RoleARN: "arn:aws:iam::222222222222:role/lazycloud",
	})
	if err != nil {
		t.Fatal(err)
	}
	if existing.Pending == nil || existing.Pending.Mode != compute.ModeExistingRole || len(existing.Pending.ExternalID()) < 32 || o.compute.View(existing).ConsoleURL != nil {
		t.Fatalf("existing-role connection %+v, want a pending role with a generated external ID and no stack", existing.Pending)
	}
}

func TestValidationProvesTheRole(t *testing.T) {
	t.Parallel()
	o, emulator, customer := connectionFleet(t)
	ready, denied, open, drifted := newUser(t, o.pool, "ready@example.com"), newUser(t, o.pool, "denied@example.com"),
		newUser(t, o.pool, "open@example.com"), newUser(t, o.pool, "drifted@example.com")

	conn := connected(t, o, customer, ready)
	if conn.Active == nil || conn.Active.Phase != compute.AuthReady || conn.Pending != nil || !conn.HostsWorkloads() {
		t.Fatalf("validated connection %+v", conn)
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
	if conn := validate(t, o, drifted); conn.Phase != compute.ConnAwaiting {
		t.Fatalf("stack still creating: %s %v, want awaiting_authorization", conn.Phase, code(conn.Pending))
	}
}

// A setup says where the customer's stack stands, checks again within
// seconds while the customer sets it up, and keeps waiting through a failed
// stack until the customer creates it again.
func TestASetupFollowsTheCustomersStack(t *testing.T) {
	t.Parallel()
	o, _, customer := connectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	conn := connect(t, o, alice, "111111111111")
	check := func(want compute.SetupStep) compute.Connection {
		t.Helper()
		conn := validate(t, o, alice)
		if conn.Phase != compute.ConnAwaiting || conn.Pending == nil || conn.Pending.SetupStep == nil || *conn.Pending.SetupStep != want {
			t.Fatalf("connection %s with pending %+v, want awaiting_authorization at %s", conn.Phase, conn.Pending, want)
		}
		if conn.NextRetryAt == nil || time.Until(*conn.NextRetryAt) > 15*time.Second {
			t.Fatalf("next check at %v, want within 15 seconds", conn.NextRetryAt)
		}
		return conn
	}
	check(compute.SetupWaitingForStack)

	customer.authorize(t, o, conn, role{})
	customer.setStatus(conn.Pending.StackName, "CREATE_IN_PROGRESS")
	check(compute.SetupStackCreating)

	customer.setStatus(conn.Pending.StackName, "ROLLBACK_IN_PROGRESS")
	failed := check(compute.SetupStackFailed)
	if !strings.Contains(failed.Pending.ErrorMessage, "Validation failed with 1 error(s)") {
		t.Fatalf("failed stack reported %q, want CloudFormation's reason", failed.Pending.ErrorMessage)
	}
	if view := o.compute.View(failed); view.Customer == nil || view.ConsoleURL == nil {
		t.Fatalf("failed stack offers %+v and console %v, want the stack to delete and the link to create it again", view.Customer, view.ConsoleURL)
	}
	// The rollback deletes the role; the failure stays until the customer
	// creates the stack again.
	customer.mu.Lock()
	delete(customer.roles, conn.Pending.RoleARN)
	customer.mu.Unlock()
	if again := check(compute.SetupStackFailed); again.Pending.ErrorMessage != failed.Pending.ErrorMessage {
		t.Fatalf("after the role went the failure reads %q, want %q", again.Pending.ErrorMessage, failed.Pending.ErrorMessage)
	}

	customer.authorize(t, o, failed, role{})
	if ready := validate(t, o, alice); ready.Phase != compute.ConnReady || ready.Active == nil || ready.Active.SetupStep != nil {
		t.Fatalf("after the stack was created again: %s %+v, want ready", ready.Phase, ready.Active)
	}
}

func code(a *compute.Authorization) compute.AuthorizationError {
	if a == nil || a.ErrorCode == nil {
		return ""
	}
	return *a.ErrorCode
}

func TestReconnectRetiresTheReplacedStack(t *testing.T) {
	t.Parallel()
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

// customerURL is where the connection sends the customer, or "" when it
// waits on no one.
func customerURL(c *compute.Connection) string {
	if c.Customer == nil {
		return ""
	}
	return c.Customer.URL
}

// phaseOf is the connection's phase, or "" once it is gone.
func phaseOf(t *testing.T, o owners, account identity.UserID) compute.ConnectionPhase {
	t.Helper()
	if conn := current(t, o, account); conn != nil {
		return conn.Phase
	}
	return ""
}

func deletedStacks(e *awsEmulator) []string {
	var out []string
	for _, c := range e.calls("DeleteStack") {
		out = append(out, c.Form.Get("StackName"))
	}
	return out
}

// imagedConnectionFleet is connectionFleet with a baked node image in
// us-east-2, whose launch permission the platform grants and removes.
func imagedConnectionFleet(t *testing.T) (owners, *awsEmulator, *customerAWS) {
	t.Helper()
	emulator := newAWS(t)
	o := newOwners(t, fleetConfig(emulator.fleet(compute.Fleet{
		PrincipalARN: platformPrincipal, Images: &compute.NodeImages{CPU: map[string]string{"us-east-2": "ami-0baked"}},
	})))
	emulator.on("ModifyImageAttribute", func(awsCall) awsReply { return modifyImageAttributeReply() })
	// The platform owns the current bake and an older one in us-east-2,
	// both shared with 111111111111.
	emulator.on("DescribeImages", func(call awsCall) awsReply {
		if call.Form.Get("Owner.1") != "self" || call.Form.Get("ExecutableBy.1") != "111111111111" ||
			!strings.Contains(call.Header.Get("Authorization"), "/us-east-2/") {
			return describeImagesReply()
		}
		return describeImagesReply("ami-0baked", "ami-0older")
	})
	return o, emulator, newCustomerAWS(emulator)
}

// unshared reports whether the platform removed 111111111111's launch
// permission on every image it shared, current and older bakes alike.
func unshared(e *awsEmulator) bool {
	var removed []string
	for _, c := range e.calls("ModifyImageAttribute") {
		if c.AccessKey == platformAccessKey && c.Form.Get("LaunchPermission.Remove.1.UserId") == "111111111111" {
			removed = append(removed, c.Form.Get("ImageId"))
		}
	}
	slices.Sort(removed)
	return slices.Equal(removed, []string{"ami-0baked", "ami-0older"})
}

func TestDisconnectDrainsInstancesThenRemovesTheStack(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o, emulator, customer := imagedConnectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	conn := connected(t, o, customer, alice)
	ws := newWorkspace(t, o.pool, "prod", alice)
	run(t, o.pool, "update workspaces set connection_id = $1 where id = $2", conn.ID, ws)
	var conflict *compute.ConflictError
	if _, err := o.compute.Disconnect(ctx, alice); !errors.As(err, &conflict) {
		t.Fatalf("disconnect with a workspace in the account: %v, want ConflictError", err)
	}
	run(t, o.pool, "update workspaces set connection_id = null where id = $1", ws)
	run(t, o.pool, `insert into capacity_cooldowns (connection_key, region, instance_type, market, until, reason)
values ($1::text, 'us-east-2', 'm7i.large', 'spot', now() + interval '1 hour', 'refused')`, conn.ID)
	run(t, o.pool, `insert into fleet_markets (market, plan, generated_at, expires_at)
values ($1::text || '/spot', '{}', now(), now() + interval '1 hour')`, conn.ID)

	host := newHost(t, o.pool, hostSpec{Kind: compute.KindConnection, Provider: compute.ProviderAWS, Market: compute.MarketSpot,
		Region: "us-east-2", InstanceID: "i-0000000000000c001"})
	run(t, o.pool, "update hosts set connection_id = $1 where id = $2", conn.ID, uuid.UUID(host))
	var ended atomic.Bool
	emulator.on("TerminateInstances", func(call awsCall) awsReply {
		ended.Store(true)
		return terminateInstancesReply(call)
	})
	emulator.on("DescribeInstances", func(call awsCall) awsReply {
		if ended.Load() || !strings.Contains(call.Header.Get("Authorization"), "/us-east-2/") {
			return describeInstancesReply()
		}
		return describeInstancesReply(ec2Instance{ID: "i-0000000000000c001", State: "running",
			Tags: map[string]string{"lazycloud:fleet": "lazycloud-test", "lazycloud:host-id": uuid.UUID(host).String()}})
	})

	draining, err := o.compute.Disconnect(ctx, alice)
	if err != nil || draining == nil || draining.Phase != compute.ConnDraining {
		t.Fatalf("disconnect: %+v %v, want disconnect_draining", draining, err)
	}
	advance(t, o)
	if phase := phaseOf(t, o, alice); phase != compute.ConnDraining {
		t.Fatalf("with an instance left the connection is %s, want still draining", phase)
	}
	if _, err := o.compute.Retire(ctx, discard()); err != nil {
		t.Fatal(err)
	}
	actuate(t, o)
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
	advance(t, o) // the drain ends, and revoking asks CloudFormation to delete the stack
	if phase := phaseOf(t, o, alice); phase != compute.ConnVerifying {
		t.Fatalf("without instances, after DeleteStack the connection is %s, want verifying_revocation", phase)
	}
	if deletes := deletedStacks(emulator); len(deletes) != 1 || deletes[0] != conn.Active.StackName {
		t.Fatalf("DeleteStack calls %v, want the active stack", deletes)
	}
	assumed := len(emulator.calls("AssumeRole"))
	if err := o.compute.Reconcile(ctx, discard()); err != nil {
		t.Fatal(err)
	}
	for _, c := range emulator.calls("AssumeRole")[assumed:] {
		if c.Form.Get("RoleArn") == conn.Active.RoleARN {
			t.Fatal("reconcile assumed the role the removal is deleting")
		}
	}
	due(t, o, conn)
	advance(t, o)
	if phase := phaseOf(t, o, alice); phase != "" {
		t.Fatalf("after the stack is gone the connection is %s, want deleted", phase)
	}
	if !unshared(emulator) {
		t.Fatalf("ModifyImageAttribute calls %v, want the platform removing 111111111111's launch permission on ami-0baked",
			emulator.calls("ModifyImageAttribute"))
	}
	if n := scan[int](t, o.pool, `select (select count(*) from capacity_cooldowns where connection_key = $1::text) +
(select count(*) from fleet_markets where starts_with(market, $1::text))`, conn.ID); n != 0 {
		t.Fatalf("%d cooldown and market rows of the removed connection remain", n)
	}
}

// A network failure while reading a stack proves nothing about it: the
// connection keeps the stack to delete and tries again.
func TestAStackReadWithoutAnAnswerIsRetried(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o, emulator, customer := connectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	conn := connected(t, o, customer, alice)
	emulator.on("DescribeStacks", func(awsCall) awsReply { return dropped() })
	if _, err := o.compute.Disconnect(ctx, alice); err != nil {
		t.Fatal(err)
	}
	advance(t, o) // drained: nothing runs there
	advance(t, o) // DescribeStacks fails without an answer
	after := current(t, o, alice)
	if after == nil || after.Phase != compute.ConnRevoking || after.NextRetryAt == nil || len(emulator.calls("DescribeStacks")) == 0 {
		t.Fatalf("after DescribeStacks failed: %+v, want still revoking with a retry scheduled", after)
	}
	if n := scan[int](t, o.pool, "select count(*)::int from cloud_authorizations where id = $1 and stack_deleted_at is null", conn.Active.ID); n != 1 {
		t.Fatal("the stack was recorded deleted")
	}
}

// CloudFormation failing to delete a stack is asked again a bounded number
// of times, then the customer is shown the stack. Once the customer deleted
// it, retry finishes the removal.
func TestAStackThatKeepsFailingToDeleteGoesToTheCustomer(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o, emulator, customer := connectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	conn := connected(t, o, customer, alice)
	customer.mu.Lock()
	customer.failDelete[conn.Active.StackName] = true
	customer.mu.Unlock()

	if _, err := o.compute.Disconnect(ctx, alice); err != nil {
		t.Fatal(err)
	}
	for n := 0; n < 30 && phaseOf(t, o, alice) != compute.ConnActionRequire; n++ {
		due(t, o, conn)
		advance(t, o)
	}
	stuck := current(t, o, alice)
	if stuck == nil || stuck.Phase != compute.ConnActionRequire ||
		!strings.Contains(customerURL(stuck), "console.aws.amazon.com/cloudformation") || !strings.Contains(customerURL(stuck), "stackId=") {
		t.Fatalf("after DELETE_FAILED: %+v, want action_required with the stack's console URL", stuck)
	}
	if deletes := deletedStacks(emulator); len(deletes) < 3 {
		t.Fatalf("DeleteStack calls %v, want the failed deletion asked again before the customer is", deletes)
	}
	if actions := stuck.Actions(); !slices.Equal(actions, []compute.ConnectionAction{compute.ActionRetry, compute.ActionForget}) {
		t.Fatalf("actions %v, want retry and forget", actions)
	}

	customer.deleteStack(conn.Active.StackName)
	retried, err := o.compute.Retry(ctx, alice)
	if err != nil || retried.Phase != compute.ConnDraining {
		t.Fatalf("retry: %+v %v, want the removal started over", retried, err)
	}
	advance(t, o)
	advance(t, o)
	if phase := phaseOf(t, o, alice); phase != "" {
		t.Fatalf("after retry the connection is %s, want deleted", phase)
	}
}

// A role the customer deleted while instances still run leaves them to the
// customer; forgetting the connection then removes the image's launch
// permission and lists what may remain in AWS.
func TestARevokedRoleDuringTheDrainEndsInForget(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o, emulator, customer := imagedConnectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	conn := connected(t, o, customer, alice)
	host := newHost(t, o.pool, hostSpec{Kind: compute.KindConnection, Provider: compute.ProviderAWS, Market: compute.MarketSpot,
		Region: "us-east-2", InstanceID: "i-0000000000000d001"})
	run(t, o.pool, "update hosts set connection_id = $1 where id = $2", conn.ID, uuid.UUID(host))
	customer.deleteRole(conn.Active.RoleARN)

	if _, err := o.compute.Disconnect(ctx, alice); err != nil {
		t.Fatal(err)
	}
	var conflict *compute.ConflictError
	if _, err := o.compute.Forget(ctx, alice); !errors.As(err, &conflict) {
		t.Fatalf("forget while draining: %v, want ConflictError", err)
	}
	if _, err := o.compute.Retire(ctx, discard()); err != nil {
		t.Fatal(err)
	}
	advance(t, o)
	stuck := current(t, o, alice)
	if stuck == nil || stuck.Phase != compute.ConnActionRequire || !strings.Contains(customerURL(stuck), "console.aws.amazon.com/ec2") ||
		!slices.Equal(stuck.Actions(), []compute.ConnectionAction{compute.ActionRetry, compute.ActionForget}) {
		t.Fatalf("with the role gone mid-drain: %+v, want action_required showing the instances, with retry and forget", stuck)
	}

	left, err := o.compute.Forget(ctx, alice)
	if err != nil {
		t.Fatal(err)
	}
	want := map[compute.LeftoverKind]string{compute.LeftoverStack: conn.Active.StackID, compute.LeftoverInstance: "i-0000000000000d001"}
	if len(left) != 2 {
		t.Fatalf("left in AWS %+v, want the stack and the instance", left)
	}
	for _, l := range left {
		if want[l.Kind] != l.ID || l.Region != "us-east-2" || !strings.Contains(l.ConsoleURL, "console.aws.amazon.com") {
			t.Errorf("leftover %+v, want %s %s in us-east-2 with a console link", l, l.Kind, want[l.Kind])
		}
	}
	if phase := phaseOf(t, o, alice); phase != "" || !unshared(emulator) {
		t.Fatalf("after forget the connection is %q with ModifyImageAttribute calls %v, want it deleted and the launch permission removed",
			phase, emulator.calls("ModifyImageAttribute"))
	}
	if phase, _ := hostPhase(t, o.pool, host); phase != string(compute.PhaseFailed) {
		t.Fatalf("the forgotten connection's host is %s, want failed", phase)
	}
}

// Disconnecting mid-reconnect deletes the replacement's stack, which the
// customer created, then the active one, through the active role.
func TestDisconnectMidReconnectDeletesBothGenerationsStacks(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o, emulator, customer := connectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	first := connected(t, o, customer, alice)
	reconnecting, err := o.compute.Reconnect(ctx, alice, "")
	if err != nil {
		t.Fatal(err)
	}
	customer.authorize(t, o, reconnecting, role{})

	if _, err := o.compute.Disconnect(ctx, alice); err != nil {
		t.Fatal(err)
	}
	for n := 0; n < 10 && phaseOf(t, o, alice) != ""; n++ {
		due(t, o, first)
		advance(t, o)
	}
	if phase := phaseOf(t, o, alice); phase != "" {
		t.Fatalf("the connection is %s, want deleted", phase)
	}
	deletes := emulator.calls("DeleteStack")
	if got := deletedStacks(emulator); !slices.Equal(got, []string{reconnecting.Pending.StackName, first.Active.StackName}) {
		t.Fatalf("DeleteStack calls %v, want the replacement's stack, then the active one", got)
	}
	for _, d := range deletes {
		if d.AccessKey != assumedKey(first.Active.RoleARN) {
			t.Errorf("DeleteStack %s through %s, want the active role", d.Form.Get("StackName"), d.AccessKey)
		}
	}
}

// Removing a setup that never finished deletes the stack the customer
// created, and removes a setup whose stack AWS does not show at once.
func TestAnUnfinishedSetupIsRemovedWithTheStackTheCustomerCreates(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o, emulator, customer := connectionFleet(t)
	bob, carol := newUser(t, o.pool, "bob@example.com"), newUser(t, o.pool, "carol@example.com")
	connect(t, o, bob, "222222222222")
	creating := connect(t, o, carol, "333333333333")
	for _, account := range []identity.UserID{bob, carol} {
		if conn, err := o.compute.Disconnect(ctx, account); err != nil || conn == nil || conn.Phase != compute.ConnRevoking {
			t.Fatalf("disconnect an unfinished setup: %+v %v, want revoking", conn, err)
		}
	}
	customer.authorize(t, o, creating, role{})
	advance(t, o)
	if phase := phaseOf(t, o, bob); phase != "" {
		t.Fatalf("connection %q with no stack in AWS, want removed", phase)
	}
	for n := 0; n < 5 && phaseOf(t, o, carol) != ""; n++ {
		run(t, o.pool, "update cloud_connections set next_step_at = now()")
		advance(t, o)
	}
	if phase := phaseOf(t, o, carol); phase != "" {
		t.Fatalf("connection %q remains, want removed", phase)
	}
	if got := deletedStacks(emulator); !slices.Equal(got, []string{creating.Pending.StackName}) {
		t.Fatalf("DeleteStack calls %v, want only the stack the customer created", got)
	}
}

// Cancelling a reconnect deletes the replacement's stack the customer
// created, through the active role, which keeps serving.
func TestACancelledReconnectDeletesTheReplacementsStack(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o, emulator, customer := connectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	first := connected(t, o, customer, alice)
	reconnecting, err := o.compute.Reconnect(ctx, alice, "")
	if err != nil {
		t.Fatal(err)
	}
	customer.authorize(t, o, reconnecting, role{})

	cancelled, err := o.compute.CancelReconnect(ctx, alice)
	if err != nil || !cancelled.HostsWorkloads() {
		t.Fatalf("cancel reconnect: %+v %v, want the account still hosting", cancelled, err)
	}
	for n := 0; n < 5 && phaseOf(t, o, alice) != compute.ConnReady; n++ {
		due(t, o, first)
		advance(t, o)
	}
	conn := current(t, o, alice)
	if conn == nil || conn.Phase != compute.ConnReady || conn.Active.Generation != 1 || conn.Pending != nil || conn.Retiring != nil {
		t.Fatalf("after cancelling: %+v, want ready on the first generation", conn)
	}
	deletes := emulator.calls("DeleteStack")
	if len(deletes) != 1 || deletes[0].Form.Get("StackName") != reconnecting.Pending.StackName ||
		deletes[0].AccessKey != assumedKey(first.Active.RoleARN) {
		t.Fatalf("DeleteStack calls %v, want the replacement's stack deleted through the active role", deletes)
	}
}

// Scheduler passes claim one due connection at a time, so each step runs
// once under its own lease: while one pass runs a step, another runs the
// next connection's and never the same one.
func TestConcurrentPassesRunEachConnectionStepOnce(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o, emulator, customer := connectionFleet(t)
	alice, bob := newUser(t, o.pool, "alice@example.com"), newUser(t, o.pool, "bob@example.com")
	customer.authorize(t, o, connect(t, o, alice, "111111111111"), role{})
	validate(t, o, alice)
	customer.authorize(t, o, connect(t, o, bob, "222222222222"), role{})
	validate(t, o, bob)
	for _, account := range []identity.UserID{alice, bob} {
		if _, err := o.compute.Disconnect(ctx, account); err != nil {
			t.Fatal(err)
		}
	}

	entered, release := make(chan struct{}), make(chan struct{})
	var mu sync.Mutex
	var read []string
	emulator.on("DescribeStacks", func(call awsCall) awsReply {
		mu.Lock()
		read = append(read, call.Form.Get("StackName"))
		first := len(read) == 1
		mu.Unlock()
		if first {
			close(entered)
			<-release
		}
		return queryError(http.StatusBadRequest, "ValidationError", "Stack with id "+call.Form.Get("StackName")+" does not exist")
	})
	done := make(chan error, 1)
	go func() {
		_, err := o.compute.AdvanceConnections(ctx, discard())
		done <- err
	}()
	<-entered
	second, err := o.compute.AdvanceConnections(ctx, discard())
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if err != nil || second != 1 || len(read) != 2 || read[0] == read[1] {
		t.Fatalf("the second pass ran %d steps (%v) and the stacks read were %v, want it to run the other connection's step once",
			second, err, read)
	}
}

// A drain whose role still works waits for its instances without asking
// the customer, until the fleet's drain bound after the disconnect.
func TestADrainWaitsForItsInstancesUntilItsBound(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o, emulator, customer := connectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	conn := connected(t, o, customer, alice)
	host := newHost(t, o.pool, hostSpec{Kind: compute.KindConnection, Provider: compute.ProviderAWS, Market: compute.MarketSpot,
		Region: "us-east-2", InstanceID: "i-0000000000000e001"})
	run(t, o.pool, "update hosts set connection_id = $1 where id = $2", conn.ID, uuid.UUID(host))
	if _, err := o.compute.Disconnect(ctx, alice); err != nil {
		t.Fatal(err)
	}
	for range 20 {
		due(t, o, conn)
		advance(t, o)
	}
	if phase := phaseOf(t, o, alice); phase != compute.ConnDraining {
		t.Fatalf("after 20 steps with an instance ending the connection is %s, want still draining", phase)
	}
	// A replica whose drain bound has passed asks the customer.
	short := compute.NewCompute(o.pool, o.execution, fleetConfig(emulator.fleet(compute.Fleet{
		PrincipalARN: platformPrincipal, BucketPrefix: "lazycloud-ws-test", DrainWait: time.Nanosecond,
	})))
	due(t, o, conn)
	if _, err := short.AdvanceConnections(ctx, discard()); err != nil {
		t.Fatal(err)
	}
	if stuck := current(t, o, alice); stuck == nil || stuck.Phase != compute.ConnActionRequire || !strings.Contains(customerURL(stuck), "console.aws.amazon.com/ec2") {
		t.Fatalf("past the drain bound: %+v, want action_required showing the instances", stuck)
	}
}

// Connecting again while the account's connection is being removed is
// refused rather than handed the connection being deleted.
func TestConnectingWhileTheConnectionIsRemovedIsRefused(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o, _, _ := connectionFleet(t)
	bob := newUser(t, o.pool, "bob@example.com")
	connect(t, o, bob, "222222222222")
	if conn, err := o.compute.Disconnect(ctx, bob); err != nil || conn == nil {
		t.Fatalf("disconnect: %+v %v, want the removal under way", conn, err)
	}
	var conflict *compute.ConflictError
	if _, err := o.compute.Connect(ctx, bob, compute.ConnectRequest{AWSAccountID: "222222222222"}); !errors.As(err, &conflict) {
		t.Fatalf("connect while removing: %v, want ConflictError", err)
	}
}

// A connection whose retirement waits on the customer takes no new
// workspace.
func TestAStuckRetirementTakesNoNewWork(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o, _, customer := connectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	connected(t, o, customer, alice)
	reconnecting, err := o.compute.Reconnect(ctx, alice, "")
	if err != nil {
		t.Fatal(err)
	}
	customer.authorize(t, o, reconnecting, role{})
	replaced := validate(t, o, alice)
	customer.deleteRole(replaced.Active.RoleARN)
	advance(t, o)
	stuck := current(t, o, alice)
	if stuck == nil || stuck.Phase != compute.ConnActionRequire || stuck.Retiring == nil || stuck.HostsWorkloads() {
		t.Fatalf("with the new role gone mid-retirement: %+v, want action_required taking no new work", stuck)
	}

	tx, err := o.pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var conflict *compute.ConflictError
	if _, err := o.compute.PlaceWorkspace(ctx, tx, alice); !errors.As(err, &conflict) {
		t.Fatalf("place a workspace: %v, want ConflictError", err)
	}
}

// A host a planning pass requested just before the disconnect is never
// launched into the disconnecting account.
func TestALaunchRacingADisconnectDoesNotLaunch(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	emulator := newAWS(t)
	o := newOwners(t, fleetConfig(emulator.fleet(compute.Fleet{PrincipalARN: platformPrincipal})))
	publish(t, o.compute)
	customer := newCustomerAWS(emulator)
	emulator.on("RunInstances", launched(t))
	alice := newUser(t, o.pool, "alice@example.com")
	conn := connected(t, o, customer, alice)
	host := newHost(t, o.pool, hostSpec{Kind: compute.KindConnection, Provider: compute.ProviderAWS, Phase: compute.PhaseRequested,
		Market: compute.MarketSpot, Region: "us-east-2", Zone: "us-east-2a", ZoneID: "use2-az1", Unenrolled: true})
	run(t, o.pool, "update hosts set connection_id = $1, instance_type = 'm7i.large' where id = $2", conn.ID, uuid.UUID(host))

	if _, err := o.compute.Disconnect(ctx, alice); err != nil {
		t.Fatal(err)
	}
	if n := launch(t, o); n != 0 || len(emulator.calls("RunInstances")) != 0 {
		t.Fatalf("launched %d with %d RunInstances calls into a disconnecting account", n, len(emulator.calls("RunInstances")))
	}
	advance(t, o)
	if phase := phaseOf(t, o, alice); phase == compute.ConnDraining {
		t.Fatal("the drain still waits on the host that never launched")
	}
}

// Creating a workspace in the connection holds the connection from the
// check that it hosts workloads until the workspace commits, so a
// disconnect in between waits and then finds the workspace.
func TestAWorkspaceCreatedDuringADisconnectBlocksIt(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o, _, customer := connectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	connected(t, o, customer, alice)

	disconnected := make(chan error, 1)
	place := func(ctx context.Context, tx pgx.Tx) (uuid.UUID, error) {
		id, err := o.compute.PlaceWorkspace(ctx, tx, alice)
		if err != nil {
			return id, err
		}
		go func() {
			_, err := o.compute.Disconnect(ctx, alice)
			disconnected <- err
		}()
		// The workspace is created once the disconnect waits on its lock.
		waiting, cancel := context.WithTimeout(ctx, time.Minute)
		defer cancel()
		tick := time.NewTicker(10 * time.Millisecond)
		defer tick.Stop()
		for {
			var blocked bool
			if err := o.pool.QueryRow(waiting, "select exists (select 1 from pg_stat_activity where datname = current_database() and wait_event_type = 'Lock')").Scan(&blocked); err != nil {
				return id, fmt.Errorf("read lock waits: %w", err)
			}
			if blocked {
				return id, nil
			}
			select {
			case <-waiting.Done():
				return id, errors.New("the disconnect never waited for the workspace being created")
			case <-tick.C:
			}
		}
	}
	admin := identity.Principal{User: alice, IsAdmin: true}
	if _, err := identity.NewIdentity(o.pool, identity.Config{}).CreateOwnedWorkspace(ctx, admin, "prod", place); err != nil {
		t.Fatal(err)
	}
	var conflict *compute.ConflictError
	if err := <-disconnected; !errors.As(err, &conflict) {
		t.Fatalf("disconnect racing the workspace: %v, want ConflictError", err)
	}
}

// A disconnect whose role AWS refuses before the platform asked to delete
// its stack, as once the customer deleted the stack themselves, finishes:
// the platform can reach nothing there, so nothing is left for it to ask.
func TestARemovalWhoseRoleIsGoneFinishes(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	o, emulator, customer := imagedConnectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	conn := connected(t, o, customer, alice)
	customer.deleteStack(conn.Active.StackName)

	if _, err := o.compute.Disconnect(ctx, alice); err != nil {
		t.Fatal(err)
	}
	advance(t, o)
	if phase := phaseOf(t, o, alice); phase != "" || len(emulator.calls("DeleteStack")) != 0 || !unshared(emulator) {
		t.Fatalf("connection %q with %d DeleteStack calls and image calls %v, want it removed with the launch permission",
			phase, len(emulator.calls("DeleteStack")), emulator.calls("ModifyImageAttribute"))
	}
}

// Revalidating a working connection keeps it taking work: a launch and a
// new workspace go ahead while the check runs.
func TestAConnectionTakesWorkWhileItsRoleIsRevalidated(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	emulator := newAWS(t)
	o := newOwners(t, fleetConfig(emulator.fleet(compute.Fleet{PrincipalARN: platformPrincipal})))
	publish(t, o.compute)
	customer := newCustomerAWS(emulator)
	emulator.on("RunInstances", launched(t))
	alice := newUser(t, o.pool, "alice@example.com")
	conn := connected(t, o, customer, alice)

	identify := emulator.handler("GetCallerIdentity")
	entered, release := make(chan struct{}), make(chan struct{})
	var enter, leave sync.Once
	defer leave.Do(func() { close(release) })
	emulator.on("GetCallerIdentity", func(call awsCall) awsReply {
		enter.Do(func() {
			close(entered)
			<-release
		})
		return identify(call)
	})
	validated := make(chan error, 1)
	go func() {
		_, err := o.compute.Validate(ctx, alice)
		validated <- err
	}()
	<-entered
	if phase := phaseOf(t, o, alice); phase != compute.ConnValidating {
		t.Fatalf("while the check runs the connection is %s, want validating", phase)
	}
	host := newHost(t, o.pool, hostSpec{Kind: compute.KindConnection, Provider: compute.ProviderAWS, Phase: compute.PhaseRequested,
		Market: compute.MarketSpot, Region: "us-east-2", Zone: "us-east-2a", ZoneID: "use2-az1", Unenrolled: true})
	run(t, o.pool, "update hosts set connection_id = $1, instance_type = 'm7i.large' where id = $2", conn.ID, uuid.UUID(host))
	if n := launch(t, o); n != 1 {
		t.Fatalf("launched %d while the role was revalidated, want 1", n)
	}
	tx, err := o.pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	_, placed := o.compute.PlaceWorkspace(ctx, tx, alice)
	if err := tx.Rollback(ctx); err != nil {
		t.Fatal(err)
	}
	if placed != nil {
		t.Fatalf("place a workspace while the role was revalidated: %v", placed)
	}
	leave.Do(func() { close(release) })
	if err := <-validated; err != nil {
		t.Fatal(err)
	}
}
