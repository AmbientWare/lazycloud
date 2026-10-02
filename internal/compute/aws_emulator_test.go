package compute_test

import (
	"fmt"
	"html"
	"net/http"
	"net/http/httptest"
	"net/url"
	"regexp"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// platformAccessKey signs calls made with the platform's own credentials.
const platformAccessKey = "AKIAPLATFORM0000TEST"

// awsEmulator answers the AWS Query APIs compute calls, EC2 2016-11-15, STS
// 2011-06-15 and CloudFormation 2010-05-15, in the XML shapes AWS returns.
// It routes by the form Action to handlers each test installs, records every
// call, and fails the test on an action without a handler.
type awsEmulator struct {
	t   *testing.T
	url string

	mu       sync.Mutex
	handlers map[string]awsHandler
	log      []awsCall
}

// awsCall is one request the emulator received.
type awsCall struct {
	Action string
	Form   url.Values
	Header http.Header
	// Host is the Host header, which an identity proof keeps from its
	// original STS URL.
	Host string
	// AccessKey is the access key id in the request's SigV4 credential.
	AccessKey string
}

type awsReply struct {
	status int
	body   string
}

type awsHandler func(awsCall) awsReply

func newAWS(t *testing.T) *awsEmulator {
	t.Helper()
	a := &awsEmulator{t: t, handlers: map[string]awsHandler{}}
	server := httptest.NewServer(http.HandlerFunc(a.serve))
	t.Cleanup(server.Close)
	a.url = server.URL
	return a
}

var credentialPattern = regexp.MustCompile(`Credential=([^/,\s]+)/`) //nolint:gochecknoglobals // Compiled once for the emulator.

func (a *awsEmulator) serve(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseForm(); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	call := awsCall{Action: r.Form.Get("Action"), Form: r.Form, Header: r.Header.Clone(), Host: r.Host}
	if m := credentialPattern.FindStringSubmatch(r.Header.Get("Authorization")); m != nil {
		call.AccessKey = m[1]
	}
	a.mu.Lock()
	a.log = append(a.log, call)
	handler := a.handlers[call.Action]
	a.mu.Unlock()
	reply := awsReply{status: http.StatusBadRequest, body: queryErrorBody("InvalidAction", "no handler for "+call.Action)}
	if handler == nil {
		a.t.Errorf("unexpected AWS call %s: %v", call.Action, call.Form)
	} else {
		reply = handler(call)
	}
	w.Header().Set("Content-Type", "text/xml")
	w.Header().Set("X-Amzn-Requestid", "req-0000")
	w.WriteHeader(reply.status)
	_, _ = w.Write([]byte(reply.body))
}

// on answers action with h from now on.
func (a *awsEmulator) on(action string, h awsHandler) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.handlers[action] = h
}

// calls returns the recorded calls of action in arrival order.
func (a *awsEmulator) calls(action string) []awsCall {
	a.mu.Lock()
	defer a.mu.Unlock()
	var out []awsCall
	for _, c := range a.log {
		if c.Action == action {
			out = append(out, c)
		}
	}
	return out
}

// fleet points f at the emulator with static platform credentials. The SDK
// does not retry, so each launch or check is one call.
func (a *awsEmulator) fleet(f compute.Fleet) compute.Fleet {
	f.AWS = aws.Config{
		Region:      "us-east-2",
		Credentials: credentials.NewStaticCredentialsProvider(platformAccessKey, "platform-secret", ""),
		Retryer:     func() aws.Retryer { return aws.NopRetryer{} },
	}
	f.Endpoints = compute.Endpoints{EC2: a.url, STS: a.url, CloudFormation: a.url}
	return f
}

// list reads a query-protocol list such as InstanceId.1, InstanceId.2.
func list(form url.Values, prefix string) []string {
	var out []string
	for n := 1; ; n++ {
		v, ok := form[fmt.Sprintf("%s.%d", prefix, n)]
		if !ok {
			return out
		}
		out = append(out, v...)
	}
}

// instanceTags reads the instance tag specification of a RunInstances call.
func instanceTags(form url.Values) map[string]string {
	for s := 1; ; s++ {
		prefix := fmt.Sprintf("TagSpecification.%d.", s)
		kind, ok := form[prefix+"ResourceType"]
		if !ok {
			return nil
		}
		if kind[0] != "instance" {
			continue
		}
		tags := map[string]string{}
		for n := 1; ; n++ {
			key := form.Get(fmt.Sprintf("%sTag.%d.Key", prefix, n))
			if key == "" {
				return tags
			}
			tags[key] = form.Get(fmt.Sprintf("%sTag.%d.Value", prefix, n))
		}
	}
}

func esc(s string) string { return html.EscapeString(s) }

func ok(body string) awsReply { return awsReply{status: http.StatusOK, body: body} }

// ec2Error is EC2's error document.
func ec2Error(status int, code, message string) awsReply {
	return awsReply{status: status, body: fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<Response><Errors><Error><Code>%s</Code><Message>%s</Message></Error></Errors><RequestID>req-0000</RequestID></Response>`,
		esc(code), esc(message))}
}

func queryErrorBody(code, message string) string {
	return fmt.Sprintf(`<ErrorResponse><Error><Type>Sender</Type><Code>%s</Code><Message>%s</Message></Error><RequestId>req-0000</RequestId></ErrorResponse>`,
		esc(code), esc(message))
}

// queryError is the error document of STS and CloudFormation.
func queryError(status int, code, message string) awsReply {
	return awsReply{status: status, body: queryErrorBody(code, message)}
}

func runInstancesReply(instanceID, instanceType, zone string) awsReply {
	return ok(fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<RunInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">
  <requestId>req-0000</requestId>
  <reservationId>r-0f00000000000000a</reservationId>
  <ownerId>111122223333</ownerId>
  <groupSet/>
  <instancesSet>
    <item>
      <instanceId>%s</instanceId>
      <imageId>ami-0abcdef1234567890</imageId>
      <instanceState><code>0</code><name>pending</name></instanceState>
      <instanceType>%s</instanceType>
      <placement><availabilityZone>%s</availabilityZone><tenancy>default</tenancy></placement>
    </item>
  </instancesSet>
</RunInstancesResponse>`, esc(instanceID), esc(instanceType), esc(zone)))
}

// ec2Instance is one instance DescribeInstances reports.
type ec2Instance struct {
	ID    string
	State string
	Tags  map[string]string
}

func instanceStateCode(state string) int {
	switch state {
	case "running":
		return 16
	case "shutting-down":
		return 32
	case "terminated":
		return 48
	case "stopping":
		return 64
	case "stopped":
		return 80
	}
	return 0
}

func describeInstancesReply(instances ...ec2Instance) awsReply {
	var b strings.Builder
	b.WriteString(`<?xml version="1.0" encoding="UTF-8"?>
<DescribeInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><requestId>req-0000</requestId><reservationSet>`)
	for _, i := range instances {
		fmt.Fprintf(&b, `<item><reservationId>r-%s</reservationId><ownerId>111122223333</ownerId><instancesSet><item>`+
			`<instanceId>%s</instanceId><instanceState><code>%d</code><name>%s</name></instanceState><tagSet>`,
			esc(strings.TrimPrefix(i.ID, "i-")), esc(i.ID), instanceStateCode(i.State), esc(i.State))
		keys := make([]string, 0, len(i.Tags))
		for k := range i.Tags {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			fmt.Fprintf(&b, `<item><key>%s</key><value>%s</value></item>`, esc(k), esc(i.Tags[k]))
		}
		b.WriteString(`</tagSet></item></instancesSet></item>`)
	}
	b.WriteString(`</reservationSet></DescribeInstancesResponse>`)
	return ok(b.String())
}

func terminateInstancesReply(call awsCall) awsReply {
	var b strings.Builder
	b.WriteString(`<?xml version="1.0" encoding="UTF-8"?>
<TerminateInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><requestId>req-0000</requestId><instancesSet>`)
	for _, id := range list(call.Form, "InstanceId") {
		fmt.Fprintf(&b, `<item><instanceId>%s</instanceId><currentState><code>32</code><name>shutting-down</name></currentState>`+
			`<previousState><code>16</code><name>running</name></previousState></item>`, esc(id))
	}
	b.WriteString(`</instancesSet></TerminateInstancesResponse>`)
	return ok(b.String())
}

func describeSubnetsReply(vpc string, subnets ...compute.Subnet) awsReply {
	var b strings.Builder
	b.WriteString(`<?xml version="1.0" encoding="UTF-8"?>
<DescribeSubnetsResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><requestId>req-0000</requestId><subnetSet>`)
	for _, s := range subnets {
		fmt.Fprintf(&b, `<item><subnetId>%s</subnetId><state>available</state><vpcId>%s</vpcId>`+
			`<availabilityZone>%s</availabilityZone><availabilityZoneId>%s</availabilityZoneId></item>`,
			esc(s.ID), esc(vpc), esc(s.Zone), esc(s.ZoneID))
	}
	b.WriteString(`</subnetSet></DescribeSubnetsResponse>`)
	return ok(b.String())
}

func assumeRoleReply(accessKey, roleARN, session string) awsReply {
	account := strings.Split(roleARN, ":")[4]
	role := roleARN[strings.LastIndex(roleARN, "/")+1:]
	return ok(fmt.Sprintf(`<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <AssumeRoleResult>
    <AssumedRoleUser>
      <AssumedRoleId>AROA3XFRBF535EXAMPLE:%[4]s</AssumedRoleId>
      <Arn>arn:aws:sts::%[2]s:assumed-role/%[3]s/%[4]s</Arn>
    </AssumedRoleUser>
    <Credentials>
      <AccessKeyId>%[1]s</AccessKeyId>
      <SecretAccessKey>assumed-secret</SecretAccessKey>
      <SessionToken>assumed-session-token</SessionToken>
      <Expiration>%[5]s</Expiration>
    </Credentials>
  </AssumeRoleResult>
  <ResponseMetadata><RequestId>req-0000</RequestId></ResponseMetadata>
</AssumeRoleResponse>`, esc(accessKey), esc(account), esc(role), esc(session), time.Now().Add(time.Hour).UTC().Format(time.RFC3339)))
}

func callerIdentityReply(account, arn, userID string) awsReply {
	return ok(fmt.Sprintf(`<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <GetCallerIdentityResult>
    <Arn>%s</Arn>
    <UserId>%s</UserId>
    <Account>%s</Account>
  </GetCallerIdentityResult>
  <ResponseMetadata><RequestId>req-0000</RequestId></ResponseMetadata>
</GetCallerIdentityResponse>`, esc(arn), esc(userID), esc(account)))
}

// cfnStack is one stack DescribeStacks reports.
type cfnStack struct {
	Name    string
	ID      string
	Status  string
	Outputs map[string]string
}

func describeStacksReply(s cfnStack) awsReply {
	var b strings.Builder
	fmt.Fprintf(&b, `<DescribeStacksResponse xmlns="http://cloudformation.amazonaws.com/doc/2010-05-15/"><DescribeStacksResult><Stacks><member>`+
		`<StackName>%s</StackName><StackId>%s</StackId><StackStatus>%s</StackStatus><CreationTime>2026-09-30T12:00:00Z</CreationTime><Outputs>`,
		esc(s.Name), esc(s.ID), esc(s.Status))
	keys := make([]string, 0, len(s.Outputs))
	for k := range s.Outputs {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		fmt.Fprintf(&b, `<member><OutputKey>%s</OutputKey><OutputValue>%s</OutputValue></member>`, esc(k), esc(s.Outputs[k]))
	}
	b.WriteString(`</Outputs></member></Stacks></DescribeStacksResult><ResponseMetadata><RequestId>req-0000</RequestId></ResponseMetadata></DescribeStacksResponse>`)
	return ok(b.String())
}

func deleteStackReply() awsReply {
	return ok(`<DeleteStackResponse xmlns="http://cloudformation.amazonaws.com/doc/2010-05-15/"><ResponseMetadata><RequestId>req-0000</RequestId></ResponseMetadata></DeleteStackResponse>`)
}

func modifyImageAttributeReply() awsReply {
	return ok(`<ModifyImageAttributeResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><requestId>req-0000</requestId><return>true</return></ModifyImageAttributeResponse>`)
}
