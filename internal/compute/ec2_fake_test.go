package compute_test

import (
	"fmt"
	"net/http"
	"net/url"
	"slices"
	"sort"
	"strings"
	"sync"
	"time"
)

// fakeEC2 is a small EC2 region on the emulator: instances and Spot
// requests that StopInstances, StartInstances, TerminateInstances,
// CancelSpotInstanceRequests and RunInstances change, and the describe
// calls with the filters compute sends. A refuse hook answers a call with
// an error instead.
type fakeEC2 struct {
	mu        sync.Mutex
	instances map[string]*fakeInstance
	requests  map[string]*fakeSpotRequest
	refuse    func(call awsCall) (awsReply, bool)
	launched  int
}

type fakeInstance struct {
	ID          string
	State       string
	Type        string
	Launched    time.Time
	Hibernation bool
	SpotRequest string
	StateReason string
	Tags        map[string]string
}

type fakeSpotRequest struct {
	ID       string
	State    string
	Instance string
	Tags     map[string]string
}

func newFakeEC2(e *awsEmulator) *fakeEC2 {
	f := &fakeEC2{instances: map[string]*fakeInstance{}, requests: map[string]*fakeSpotRequest{}}
	for action, h := range map[string]func(awsCall) awsReply{
		"DescribeInstances":            f.describeInstances,
		"StopInstances":                f.stop,
		"StartInstances":               f.start,
		"TerminateInstances":           f.terminate,
		"DescribeSpotInstanceRequests": f.describeRequests,
		"CancelSpotInstanceRequests":   f.cancel,
		"RunInstances":                 f.run,
	} {
		e.on(action, func(call awsCall) awsReply {
			f.mu.Lock()
			defer f.mu.Unlock()
			if f.refuse != nil {
				if reply, refused := f.refuse(call); refused {
					return reply
				}
			}
			return h(call)
		})
	}
	return f
}

// add puts an instance in the region, tagged for the test fleet and host.
func (f *fakeEC2) add(i fakeInstance, host string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if i.Tags == nil {
		i.Tags = map[string]string{"lazycloud:fleet": "lazycloud-test", "lazycloud:host-id": host}
	}
	if i.Type == "" {
		i.Type = "m7i.large"
	}
	f.instances[i.ID] = &i
}

func (f *fakeEC2) addRequest(r fakeSpotRequest, host string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.Tags == nil {
		r.Tags = map[string]string{"lazycloud:fleet": "lazycloud-test", "lazycloud:host-id": host}
	}
	f.requests[r.ID] = &r
}

func (f *fakeEC2) set(id string, update func(*fakeInstance)) {
	f.mu.Lock()
	defer f.mu.Unlock()
	update(f.instances[id])
}

func (f *fakeEC2) get(id string) fakeInstance {
	f.mu.Lock()
	defer f.mu.Unlock()
	return *f.instances[id]
}

func (f *fakeEC2) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.instances)
}

func (f *fakeEC2) request(id string) fakeSpotRequest {
	f.mu.Lock()
	defer f.mu.Unlock()
	return *f.requests[id]
}

// filters reads Filter.N.Name and its values.
func filters(form url.Values) map[string][]string {
	out := map[string][]string{}
	for n := 1; ; n++ {
		name := form.Get(fmt.Sprintf("Filter.%d.Name", n))
		if name == "" {
			return out
		}
		out[name] = list(form, fmt.Sprintf("Filter.%d.Value", n))
	}
}

func matches(tags map[string]string, want map[string][]string) bool {
	for name, values := range want {
		if key, ok := strings.CutPrefix(name, "tag:"); ok && !slices.Contains(values, tags[key]) {
			return false
		}
	}
	return true
}

func (f *fakeEC2) describeInstances(call awsCall) awsReply {
	want := filters(call.Form)
	ids := make([]string, 0, len(f.instances))
	for id := range f.instances {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	var b strings.Builder
	b.WriteString(`<?xml version="1.0" encoding="UTF-8"?>
<DescribeInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><requestId>req-0000</requestId><reservationSet>`)
	for _, id := range ids {
		i := f.instances[id]
		if v, ok := want["instance-id"]; ok && !slices.Contains(v, i.ID) {
			continue
		}
		if v, ok := want["spot-instance-request-id"]; ok && !slices.Contains(v, i.SpotRequest) {
			continue
		}
		if !matches(i.Tags, want) {
			continue
		}
		fmt.Fprintf(&b, `<item><reservationId>r-%s</reservationId><ownerId>111122223333</ownerId><instancesSet><item>`+
			`<instanceId>%s</instanceId><instanceType>%s</instanceType><instanceState><code>%d</code><name>%s</name></instanceState>`+
			`<launchTime>%s</launchTime><hibernationOptions><configured>%t</configured></hibernationOptions>`,
			esc(strings.TrimPrefix(i.ID, "i-")), esc(i.ID), esc(i.Type), instanceStateCode(i.State), esc(i.State),
			i.Launched.UTC().Format(time.RFC3339), i.Hibernation)
		if i.SpotRequest != "" {
			fmt.Fprintf(&b, `<spotInstanceRequestId>%s</spotInstanceRequestId><instanceLifecycle>spot</instanceLifecycle>`, esc(i.SpotRequest))
		}
		if i.StateReason != "" {
			fmt.Fprintf(&b, `<stateReason><code>%s</code><message>%s</message></stateReason>`, esc(i.StateReason), esc(i.StateReason))
		}
		b.WriteString(`<tagSet>`)
		writeTags(&b, i.Tags)
		b.WriteString(`</tagSet></item></instancesSet></item>`)
	}
	b.WriteString(`</reservationSet></DescribeInstancesResponse>`)
	return ok(b.String())
}

func writeTags(b *strings.Builder, tags map[string]string) {
	keys := make([]string, 0, len(tags))
	for k := range tags {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		fmt.Fprintf(b, `<item><key>%s</key><value>%s</value></item>`, esc(k), esc(tags[k]))
	}
}

// stateChanges answers a Stop, Start or Terminate call after moving each
// named instance with move.
func (f *fakeEC2) stateChanges(call awsCall, response string, move func(*fakeInstance)) awsReply {
	var b strings.Builder
	fmt.Fprintf(&b, `<?xml version="1.0" encoding="UTF-8"?><%s xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><requestId>req-0000</requestId><instancesSet>`, response)
	for _, id := range list(call.Form, "InstanceId") {
		i, found := f.instances[id]
		if !found {
			return ec2Error(http.StatusBadRequest, "InvalidInstanceID.NotFound", "The instance ID '"+id+"' does not exist")
		}
		before := i.State
		move(i)
		fmt.Fprintf(&b, `<item><instanceId>%s</instanceId><currentState><code>%d</code><name>%s</name></currentState>`+
			`<previousState><code>%d</code><name>%s</name></previousState></item>`,
			esc(id), instanceStateCode(i.State), esc(i.State), instanceStateCode(before), esc(before))
	}
	fmt.Fprintf(&b, `</instancesSet></%s>`, response)
	return ok(b.String())
}

func (f *fakeEC2) stop(call awsCall) awsReply {
	return f.stateChanges(call, "StopInstancesResponse", func(i *fakeInstance) {
		if i.State != "running" && i.State != "stopping" {
			return
		}
		i.State = "stopping"
		i.StateReason = "Client.UserInitiatedShutdown"
		if call.Form.Get("Hibernate") == "true" {
			i.StateReason = "Client.UserInitiatedHibernate"
		}
	})
}

func (f *fakeEC2) start(call awsCall) awsReply {
	return f.stateChanges(call, "StartInstancesResponse", func(i *fakeInstance) {
		if i.State == "stopped" {
			i.State, i.StateReason, i.Launched = "pending", "", time.Now()
		}
	})
}

func (f *fakeEC2) terminate(call awsCall) awsReply {
	return f.stateChanges(call, "TerminateInstancesResponse", func(i *fakeInstance) {
		if i.State != "terminated" {
			i.State = "shutting-down"
		}
		// A live persistent request relaunches its instance.
		if r, ok := f.requests[i.SpotRequest]; ok && r.State == "active" {
			f.launched++
			id := fmt.Sprintf("i-%017x", 0xf0000+f.launched)
			f.instances[id] = &fakeInstance{ID: id, State: "pending", Type: i.Type, Launched: time.Now(), SpotRequest: r.ID}
			r.Instance = id
		}
	})
}

func (f *fakeEC2) describeRequests(call awsCall) awsReply {
	ids := list(call.Form, "SpotInstanceRequestId")
	want := filters(call.Form)
	var b strings.Builder
	b.WriteString(`<?xml version="1.0" encoding="UTF-8"?>
<DescribeSpotInstanceRequestsResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><requestId>req-0000</requestId><spotInstanceRequestSet>`)
	keys := make([]string, 0, len(f.requests))
	for id := range f.requests {
		keys = append(keys, id)
	}
	sort.Strings(keys)
	for _, id := range ids {
		if _, found := f.requests[id]; !found {
			return ec2Error(http.StatusBadRequest, "InvalidSpotInstanceRequestID.NotFound", "The spot instance request ID '"+id+"' does not exist")
		}
	}
	for _, id := range keys {
		r := f.requests[id]
		if len(ids) > 0 && !slices.Contains(ids, id) {
			continue
		}
		if v, ok := want["state"]; ok && !slices.Contains(v, r.State) {
			continue
		}
		if !matches(r.Tags, want) {
			continue
		}
		fmt.Fprintf(&b, `<item><spotInstanceRequestId>%s</spotInstanceRequestId><state>%s</state><type>persistent</type>`+
			`<instanceId>%s</instanceId><tagSet>`, esc(r.ID), esc(r.State), esc(r.Instance))
		writeTags(&b, r.Tags)
		b.WriteString(`</tagSet></item>`)
	}
	b.WriteString(`</spotInstanceRequestSet></DescribeSpotInstanceRequestsResponse>`)
	return ok(b.String())
}

func (f *fakeEC2) cancel(call awsCall) awsReply {
	var b strings.Builder
	b.WriteString(`<?xml version="1.0" encoding="UTF-8"?>
<CancelSpotInstanceRequestsResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><requestId>req-0000</requestId><spotInstanceRequestSet>`)
	for _, id := range list(call.Form, "SpotInstanceRequestId") {
		if r, ok := f.requests[id]; ok {
			r.State = "cancelled"
		}
		fmt.Fprintf(&b, `<item><spotInstanceRequestId>%s</spotInstanceRequestId><state>cancelled</state></item>`, esc(id))
	}
	b.WriteString(`</spotInstanceRequestSet></CancelSpotInstanceRequestsResponse>`)
	return ok(b.String())
}

// run launches one instance, on a persistent Spot request when asked.
func (f *fakeEC2) run(call awsCall) awsReply {
	f.launched++
	id := fmt.Sprintf("i-%017x", 0xa0000+f.launched)
	i := &fakeInstance{
		ID: id, State: "pending", Type: call.Form.Get("InstanceType"), Launched: time.Now(),
		Hibernation: call.Form.Get("HibernationOptions.Configured") == "true", Tags: instanceTags(call.Form),
	}
	request := ""
	if call.Form.Get("InstanceMarketOptions.SpotOptions.SpotInstanceType") == "persistent" {
		request = fmt.Sprintf("sir-%08d", f.launched)
		f.requests[request] = &fakeSpotRequest{ID: request, State: "active", Instance: id, Tags: map[string]string{
			"lazycloud:fleet": i.Tags["lazycloud:fleet"], "lazycloud:host-id": i.Tags["lazycloud:host-id"],
		}}
		i.SpotRequest = request
	}
	f.instances[id] = i
	if request != "" {
		request = "<spotInstanceRequestId>" + esc(request) + "</spotInstanceRequestId>"
	}
	return ok(fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<RunInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><requestId>req-0000</requestId>
<reservationId>r-0f00000000000000a</reservationId><ownerId>111122223333</ownerId><groupSet/><instancesSet><item>
<instanceId>%s</instanceId><imageId>ami-0resolved</imageId><instanceState><code>0</code><name>pending</name></instanceState>
<instanceType>%s</instanceType><placement><availabilityZone>us-east-2a</availabilityZone><tenancy>default</tenancy></placement>
%s</item></instancesSet></RunInstancesResponse>`, esc(id), esc(i.Type), request))
}
