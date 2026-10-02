package compute_test

import (
	"net/http"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// reserveFleet is a platform fleet on a fake EC2 region.
func reserveFleet(t *testing.T) (owners, *awsEmulator, *fakeEC2) {
	t.Helper()
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	return o, emulator, newFakeEC2(emulator)
}

// reserve is a platform host in phase with an instance in us-east-2 that
// sleeps by mode, launched able to hibernate when mode is hibernate.
func reserve(t *testing.T, o owners, phase compute.Phase, market compute.Market, mode compute.ReserveMode, instance string) compute.HostID {
	t.Helper()
	host := newHost(t, o.pool, hostSpec{
		Provider: compute.ProviderAWS, Phase: phase, Market: market, Region: "us-east-2", Zone: "us-east-2a", InstanceID: instance,
	})
	run(t, o.pool, "update hosts set reserve_mode = $2, hibernation_configured = $3, instance_type = 'm7i.large' where id = $1",
		uuid.UUID(host), string(mode), mode == compute.ReserveHibernate)
	return host
}

func actuate(t *testing.T, o owners) compute.ActuateResult {
	t.Helper()
	result, err := o.compute.Actuate(t.Context(), discard())
	if err != nil {
		t.Fatalf("actuate: %v", err)
	}
	return result
}

// expireLeases lets the next pass claim hosts the last one held.
func expireLeases(t *testing.T, o owners) {
	t.Helper()
	run(t, o.pool, "update hosts set launch_lease_until = now() - interval '1 second' where launch_lease_until is not null")
}

// ago sets a timestamp column of host to d before now.
func ago(t *testing.T, o owners, host compute.HostID, column string, d time.Duration) {
	t.Helper()
	run(t, o.pool, "update hosts set "+column+" = now() - make_interval(secs => $2) where id = $1", uuid.UUID(host), d.Seconds())
}

type stopFacts struct {
	Phase     string
	Requested bool
	Forced    bool
	Refused   bool
	Stopped   bool
	Evidence  string
}

func stopOf(t *testing.T, o owners, host compute.HostID) stopFacts {
	t.Helper()
	var f stopFacts
	if err := o.pool.QueryRow(t.Context(), `
select phase, stop_requested_at is not null, force_stop_at is not null, hibernate_refused_at is not null,
       stopped_at is not null, image_evidence
from hosts where id = $1`, uuid.UUID(host)).Scan(&f.Phase, &f.Requested, &f.Forced, &f.Refused, &f.Stopped, &f.Evidence); err != nil {
		t.Fatal(err)
	}
	return f
}

func TestReserveLaunchesHibernateOnAPersistentSpotRequest(t *testing.T) {
	o, emulator, ec2 := reserveFleet(t)
	insert := func(market compute.Market, mode compute.ReserveMode, instanceType string) compute.HostID {
		return compute.HostID(scan[uuid.UUID](t, o.pool, `
insert into hosts (name, state, kind, provider, phase, cpu_millis, memory_bytes, region, instance_type, market, reserve_mode)
values ('r', 'offline', 'platform', 'aws', 'requested', 1500, 6::bigint << 30, 'us-east-2', $1, $2, $3) returning id`,
			instanceType, string(market), string(mode)))
	}
	spot := insert(compute.MarketSpot, compute.ReserveHibernate, "m7i.large")
	plain := insert(compute.MarketOnDemand, compute.ReserveStop, "m7i.large")
	// 192 GiB is over EC2's hibernation limit: it stops plainly.
	large := insert(compute.MarketOnDemand, compute.ReserveHibernate, "g4dn.12xlarge")
	if n := launch(t, o); n != 3 {
		t.Fatalf("launched %d, want 3", n)
	}
	calls := map[string]awsCall{}
	for _, c := range emulator.calls("RunInstances") {
		calls[c.Form.Get("ClientToken")] = c
	}
	s := calls[spot.String()].Form
	if s.Get("HibernationOptions.Configured") != "true" || s.Get("BlockDeviceMapping.1.Ebs.VolumeSize") != "108" ||
		s.Get("BlockDeviceMapping.1.Ebs.Encrypted") != "true" ||
		s.Get("InstanceMarketOptions.SpotOptions.SpotInstanceType") != "persistent" ||
		s.Get("InstanceMarketOptions.SpotOptions.InstanceInterruptionBehavior") != "hibernate" ||
		s.Get("TagSpecification.3.ResourceType") != "spot-instances-request" || s.Get("TagSpecification.3.Tag.2.Value") != spot.String() {
		t.Errorf("spot reserve launch %v, want hibernation, a 100+8 GiB encrypted root and a tagged persistent request", s)
	}
	for _, host := range []compute.HostID{plain, large} {
		f := calls[host.String()].Form
		if f.Get("HibernationOptions.Configured") != "" || f.Get("BlockDeviceMapping.1.Ebs.VolumeSize") != "100" ||
			f.Get("InstanceMarketOptions.MarketType") != "" {
			t.Errorf("plain reserve launch %v, want an on-demand 100 GiB root without hibernation", f)
		}
	}
	var request, image string
	var configured bool
	if err := o.pool.QueryRow(t.Context(), "select spot_request_id, node_image, hibernation_configured from hosts where id = $1",
		uuid.UUID(spot)).Scan(&request, &image, &configured); err != nil {
		t.Fatal(err)
	}
	instance := scan[string](t, o.pool, "select instance_id from hosts where id = $1", uuid.UUID(spot))
	if request == "" || request != ec2.get(instance).SpotRequest || image != "ami-0resolved" || !configured {
		t.Fatalf("spot reserve recorded request %q image %q hibernation %t", request, image, configured)
	}
	if n := scan[int](t, o.pool, "select count(*)::int from hosts where id = any($1) and spot_request_id is null and not hibernation_configured",
		[]uuid.UUID{uuid.UUID(plain), uuid.UUID(large)}); n != 2 {
		t.Fatalf("%d plain reserves recorded without a request or hibernation, want 2", n)
	}
}

func TestStoppingReserveHibernatesOnceEC2AllowsAndSettlesIntoTheReserve(t *testing.T) {
	o, emulator, ec2 := reserveFleet(t)
	host := reserve(t, o, compute.PhaseStopping, compute.MarketOnDemand, compute.ReserveHibernate, "i-0000000000000c001")
	ec2.add(fakeInstance{ID: "i-0000000000000c001", State: "running", Hibernation: true, Launched: time.Now().Add(-30 * time.Second)}, host.String())

	if r := actuate(t, o); r.Stopped != 0 || len(emulator.calls("StopInstances")) != 0 {
		t.Fatalf("stopped %+v within two minutes of start; EC2 refuses that", r)
	}
	ec2.set("i-0000000000000c001", func(i *fakeInstance) { i.Launched = time.Now().Add(-3 * time.Minute) })
	expireLeases(t, o)
	if r := actuate(t, o); r.Stopped != 1 {
		t.Fatalf("actuate %+v, want one hibernation", r)
	}
	stops := emulator.calls("StopInstances")
	if len(stops) != 1 || stops[0].Form.Get("Hibernate") != "true" {
		t.Fatalf("StopInstances %v, want one with Hibernate", stops)
	}
	if f := stopOf(t, o, host); f.Phase != "stopping" || !f.Requested || f.Evidence != string(compute.EvidenceUnknown) {
		t.Fatalf("after the hibernation %+v, want stopping with the stop recorded and the image unproven", f)
	}
	// The lease holds the host; once it passes, a stop in progress is
	// left alone.
	actuate(t, o)
	expireLeases(t, o)
	actuate(t, o)
	if n := len(emulator.calls("StopInstances")); n != 1 {
		t.Fatalf("%d StopInstances calls, want the one", n)
	}
	ec2.set("i-0000000000000c001", func(i *fakeInstance) { i.State = "stopped" })
	expireLeases(t, o)
	actuate(t, o)
	if f := stopOf(t, o, host); f.Phase != "stopped" || !f.Stopped {
		t.Fatalf("after EC2 stopped it %+v, want stopped with stopped_at", f)
	}
}

func TestHibernationRefusalsRetryForTenMinutesThenTheReserveStopsPlainly(t *testing.T) {
	o, emulator, ec2 := reserveFleet(t)
	host := reserve(t, o, compute.PhaseStopping, compute.MarketOnDemand, compute.ReserveHibernate, "i-0000000000000c002")
	ec2.add(fakeInstance{ID: "i-0000000000000c002", State: "running", Hibernation: true, Launched: time.Now().Add(-5 * time.Minute)}, host.String())
	ec2.refuse = func(call awsCall) (awsReply, bool) {
		return ec2Error(http.StatusBadRequest, "UnsupportedOperation", "The instance is not ready to hibernate yet"),
			call.Action == "StopInstances" && call.Form.Get("Hibernate") == "true"
	}

	actuate(t, o)
	if f := stopOf(t, o, host); !f.Refused || f.Requested || ec2.get("i-0000000000000c002").State != "running" {
		t.Fatalf("after a refusal %+v, want the refusal recorded and no stop", f)
	}
	expireLeases(t, o)
	actuate(t, o)
	if n := len(emulator.calls("StopInstances")); n != 2 {
		t.Fatalf("%d StopInstances calls inside the window, want two hibernation attempts", n)
	}
	ago(t, o, host, "hibernate_refused_at", 11*time.Minute)
	expireLeases(t, o)
	actuate(t, o)
	stops := emulator.calls("StopInstances")
	if len(stops) != 4 || stops[2].Form.Get("Hibernate") != "true" || stops[3].Form.Get("Hibernate") != "" {
		t.Fatalf("StopInstances %v, want a last refused hibernation then a plain stop", stops)
	}
	if f := stopOf(t, o, host); !f.Requested || f.Evidence != string(compute.EvidenceUnavailable) {
		t.Fatalf("after the plain stop %+v, want the stop recorded without an image", f)
	}
}

func TestAnInstanceThatCannotHibernateStopsPlainlyAtOnce(t *testing.T) {
	o, emulator, ec2 := reserveFleet(t)
	refused := reserve(t, o, compute.PhaseStopping, compute.MarketOnDemand, compute.ReserveHibernate, "i-0000000000000c003")
	ec2.add(fakeInstance{ID: "i-0000000000000c003", State: "running", Hibernation: true, Launched: time.Now().Add(-5 * time.Minute)}, refused.String())
	// Launched without hibernation, whatever the host asks for.
	unconfigured := reserve(t, o, compute.PhaseStopping, compute.MarketOnDemand, compute.ReserveHibernate, "i-0000000000000c004")
	ec2.add(fakeInstance{ID: "i-0000000000000c004", State: "running", Launched: time.Now().Add(-5 * time.Minute)}, unconfigured.String())
	ec2.refuse = func(call awsCall) (awsReply, bool) {
		return ec2Error(http.StatusBadRequest, "UnsupportedHibernationConfiguration", "The instance was not launched with hibernation"),
			call.Action == "StopInstances" && call.Form.Get("Hibernate") == "true"
	}
	if r := actuate(t, o); r.Stopped != 2 {
		t.Fatalf("actuate %+v, want both stopped in one pass", r)
	}
	byInstance := map[string][]string{}
	for _, c := range emulator.calls("StopInstances") {
		byInstance[c.Form.Get("InstanceId.1")] = append(byInstance[c.Form.Get("InstanceId.1")], c.Form.Get("Hibernate"))
	}
	if !slices.Equal(byInstance["i-0000000000000c003"], []string{"true", ""}) || !slices.Equal(byInstance["i-0000000000000c004"], []string{""}) {
		t.Fatalf("stops %v, want an unconfigured refusal then a plain stop, and a plain stop without an attempt", byInstance)
	}
	for _, host := range []compute.HostID{refused, unconfigured} {
		if f := stopOf(t, o, host); !f.Requested || f.Evidence != string(compute.EvidenceUnavailable) {
			t.Errorf("host %s %+v, want a plain stop recorded", host, f)
		}
	}
}

func TestAStopPendingTenMinutesIsForcedAndALostStopAnswerIsNotRepeated(t *testing.T) {
	o, emulator, ec2 := reserveFleet(t)
	stuck := reserve(t, o, compute.PhaseStopping, compute.MarketOnDemand, compute.ReserveStop, "i-0000000000000c005")
	ec2.add(fakeInstance{ID: "i-0000000000000c005", State: "stopping", Launched: time.Now().Add(-time.Hour)}, stuck.String())
	run(t, o.pool, "update hosts set stop_requested_at = now() - interval '11 minutes', image_evidence = 'unavailable' where id = $1", uuid.UUID(stuck))
	// EC2 took this stop but its answer never came back.
	lost := reserve(t, o, compute.PhaseStopping, compute.MarketOnDemand, compute.ReserveHibernate, "i-0000000000000c006")
	ec2.add(fakeInstance{ID: "i-0000000000000c006", State: "stopping", Hibernation: true, Launched: time.Now().Add(-time.Hour)}, lost.String())

	actuate(t, o)
	stops := emulator.calls("StopInstances")
	if len(stops) != 1 || stops[0].Form.Get("InstanceId.1") != "i-0000000000000c005" || stops[0].Form.Get("Force") != "true" {
		t.Fatalf("StopInstances %v, want one forced stop of the stuck instance", stops)
	}
	if f := stopOf(t, o, stuck); !f.Forced || f.Evidence != string(compute.EvidenceUnavailable) {
		t.Fatalf("stuck host %+v, want the forced stop recorded", f)
	}
	if f := stopOf(t, o, lost); !f.Requested || f.Evidence != string(compute.EvidenceUnknown) {
		t.Fatalf("host with a lost answer %+v, want its hibernation recorded", f)
	}
	expireLeases(t, o)
	actuate(t, o)
	if n := len(emulator.calls("StopInstances")); n != 1 {
		t.Fatalf("%d StopInstances calls, want no repeat inside the forced stop's window", n)
	}
}

func TestResumeStartsTheReserveOnceAndARefusedSpotStartRetiresItsRequest(t *testing.T) {
	o, emulator, ec2 := reserveFleet(t)
	host := reserve(t, o, compute.PhaseResuming, compute.MarketOnDemand, compute.ReserveHibernate, "i-0000000000000d001")
	ec2.add(fakeInstance{ID: "i-0000000000000d001", State: "stopped", Hibernation: true}, host.String())
	run(t, o.pool, "update hosts set stop_requested_at = now(), stopped_at = now(), image_evidence = 'saved' where id = $1", uuid.UUID(host))

	if r := actuate(t, o); r.Started != 1 {
		t.Fatalf("actuate %+v, want one start", r)
	}
	if f := stopOf(t, o, host); f.Phase != "resuming" || f.Requested || f.Stopped || f.Evidence != string(compute.EvidenceSaved) {
		t.Fatalf("started host %+v, want resuming with the stop facts cleared and the image evidence kept", f)
	}
	expireLeases(t, o)
	actuate(t, o)
	if n := len(emulator.calls("StartInstances")); n != 1 {
		t.Fatalf("%d StartInstances calls, want no repeat while EC2 starts it", n)
	}

	spot := reserve(t, o, compute.PhaseResuming, compute.MarketSpot, compute.ReserveStop, "i-0000000000000d002")
	ec2.add(fakeInstance{ID: "i-0000000000000d002", State: "stopped", SpotRequest: "sir-0000d002"}, spot.String())
	ec2.addRequest(fakeSpotRequest{ID: "sir-0000d002", State: "disabled", Instance: "i-0000000000000d002"}, spot.String())
	run(t, o.pool, "update hosts set spot_request_id = 'sir-0000d002' where id = $1", uuid.UUID(spot))
	ec2.refuse = func(call awsCall) (awsReply, bool) {
		return ec2Error(http.StatusInternalServerError, "InsufficientInstanceCapacity", "There is no Spot capacity available"),
			call.Action == "StartInstances" && call.Form.Get("InstanceId.1") == "i-0000000000000d002"
	}
	actuate(t, o)
	if phase, _ := hostPhase(t, o.pool, spot); phase != string(compute.PhaseTerminating) {
		t.Fatalf("refused reserve is %s, want terminating", phase)
	}
	cooled := scan[string](t, o.pool, `select region || '/' || instance_type || '/' || market from capacity_cooldowns
where until > now() and refused_at > now() - interval '1 minute'`)
	if cooled != "us-east-2/m7i.large/spot" {
		t.Fatalf("cooldown %q, want the refused offer with its refusal time", cooled)
	}
	actuate(t, o)
	if r := ec2.request("sir-0000d002"); r.State != "cancelled" {
		t.Fatalf("spot request %s, want cancelled", r.State)
	}
	if s := ec2.get("i-0000000000000d002").State; s != "shutting-down" {
		t.Fatalf("refused reserve instance %s, want shutting down", s)
	}
	actions := emulator.actions()
	cancelled, terminated := slices.Index(actions, "CancelSpotInstanceRequests"), slices.Index(actions, "TerminateInstances")
	if cancelled < 0 || terminated < cancelled {
		t.Fatalf("calls %v, want the persistent request cancelled before its instance terminates", actions)
	}
}

// actions lists every call's action in arrival order.
func (a *awsEmulator) actions() []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	out := make([]string, len(a.log))
	for n, c := range a.log {
		out[n] = c.Action
	}
	return out
}

// A persistent request whose instance was replaced while the fleet was not
// looking ends with every instance it launched.
func TestTerminatingASpotReserveEndsEveryInstanceItsRequestLaunched(t *testing.T) {
	o, _, ec2 := reserveFleet(t)
	host := reserve(t, o, compute.PhaseTerminating, compute.MarketSpot, compute.ReserveStop, "i-0000000000000d003")
	ec2.add(fakeInstance{ID: "i-0000000000000d003", State: "stopped", SpotRequest: "sir-0000d003"}, host.String())
	ec2.add(fakeInstance{ID: "i-0000000000000d004", State: "running", SpotRequest: "sir-0000d003", Tags: map[string]string{}}, "")
	ec2.addRequest(fakeSpotRequest{ID: "sir-0000d003", State: "active", Instance: "i-0000000000000d004"}, host.String())
	run(t, o.pool, "update hosts set spot_request_id = 'sir-0000d003' where id = $1", uuid.UUID(host))

	if r := actuate(t, o); r.Terminated != 1 {
		t.Fatalf("actuate %+v, want the reserve terminated", r)
	}
	for _, id := range []string{"i-0000000000000d003", "i-0000000000000d004"} {
		if s := ec2.get(id).State; s != "shutting-down" {
			t.Errorf("instance %s is %s, want shutting down", id, s)
		}
	}
	if n := ec2.count(); n != 2 {
		t.Fatalf("%d instances, want no relaunch after the cancel", n)
	}
}

func TestReconcileKeepsReservesStoppedAndCancelsOrphanSpotRequests(t *testing.T) {
	o, _, ec2 := reserveFleet(t)
	stopped := reserve(t, o, compute.PhaseStopped, compute.MarketOnDemand, compute.ReserveHibernate, "i-0000000000000e001")
	ec2.add(fakeInstance{ID: "i-0000000000000e001", State: "stopped", Hibernation: true}, stopped.String())
	settling := reserve(t, o, compute.PhaseStopping, compute.MarketOnDemand, compute.ReserveStop, "i-0000000000000e002")
	ec2.add(fakeInstance{ID: "i-0000000000000e002", State: "stopped"}, settling.String())
	run(t, o.pool, "update hosts set stop_requested_at = now() where id = $1", uuid.UUID(settling))
	// Proving itself, not asked to stop: EC2 stopped it.
	preparing := reserve(t, o, compute.PhasePreparing, compute.MarketSpot, compute.ReserveStop, "i-0000000000000e003")
	ec2.add(fakeInstance{ID: "i-0000000000000e003", State: "stopped"}, preparing.String())
	// Started by someone else long ago and never resumed.
	woken := reserve(t, o, compute.PhaseStopped, compute.MarketOnDemand, compute.ReserveStop, "i-0000000000000e004")
	ec2.add(fakeInstance{ID: "i-0000000000000e004", State: "running", Launched: time.Now().Add(-time.Hour)}, woken.String())
	// A request whose host is gone, and one whose host still lives.
	ec2.add(fakeInstance{ID: "i-0000000000000e005", State: "running", SpotRequest: "sir-0000e005"}, uuid.NewString())
	ec2.addRequest(fakeSpotRequest{ID: "sir-0000e005", State: "active", Instance: "i-0000000000000e005"}, uuid.NewString())
	live := reserve(t, o, compute.PhaseStopped, compute.MarketSpot, compute.ReserveStop, "i-0000000000000e006")
	ec2.add(fakeInstance{ID: "i-0000000000000e006", State: "stopped", SpotRequest: "sir-0000e006"}, live.String())
	ec2.addRequest(fakeSpotRequest{ID: "sir-0000e006", State: "disabled", Instance: "i-0000000000000e006"}, live.String())

	if err := o.compute.Reconcile(t.Context(), discard()); err != nil {
		t.Fatal(err)
	}
	for host, want := range map[compute.HostID]compute.Phase{
		stopped: compute.PhaseStopped, settling: compute.PhaseStopped, preparing: compute.PhaseFailed,
		woken: compute.PhaseFailed, live: compute.PhaseStopped,
	} {
		if phase, _ := hostPhase(t, o.pool, host); phase != string(want) {
			t.Errorf("host %s is %s, want %s", host, phase, want)
		}
	}
	if _, failure := hostPhase(t, o.pool, preparing); failure == nil || *failure != string(compute.FailureProviderStopped) {
		t.Errorf("preparing host failure %v, want provider_stopped", failure)
	}
	for id, want := range map[string]string{
		"i-0000000000000e001": "stopped", "i-0000000000000e003": "shutting-down", "i-0000000000000e004": "shutting-down",
		"i-0000000000000e005": "shutting-down", "i-0000000000000e006": "stopped",
	} {
		if s := ec2.get(id).State; s != want {
			t.Errorf("instance %s is %s, want %s", id, s, want)
		}
	}
	if s := ec2.request("sir-0000e005").State; s != "cancelled" {
		t.Errorf("orphan request %s, want cancelled", s)
	}
	if s := ec2.request("sir-0000e006").State; s != "disabled" {
		t.Errorf("live reserve's request %s, want kept", s)
	}
}
