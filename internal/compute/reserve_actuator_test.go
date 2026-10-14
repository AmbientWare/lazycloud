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
	t.Parallel()
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
	serving := compute.HostID(scan[uuid.UUID](t, o.pool, `
insert into hosts (name, state, kind, provider, phase, cpu_millis, memory_bytes, region, instance_type, market)
values ('s', 'offline', 'platform', 'aws', 'requested', 4000, 16::bigint << 30, 'us-east-2', 'c6a.2xlarge', 'on_demand') returning id`))
	if n := launch(t, o); n != 4 {
		t.Fatalf("launched %d, want 4", n)
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
	// Hosts that can hold a build, reserves included, get the root
	// throughput builds need.
	for host, want := range map[compute.HostID]string{spot: "125", large: "500", serving: "500"} {
		if got := calls[host.String()].Form.Get("BlockDeviceMapping.1.Ebs.Throughput"); got != want {
			t.Errorf("root throughput %s MiB/s, want %s", got, want)
		}
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
	t.Parallel()
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
	if f := stopOf(t, o, host); f.Phase != "stopped" || !f.Stopped || f.Evidence != string(compute.EvidenceSaved) {
		t.Fatalf("after EC2 stopped it %+v, want stopped with stopped_at and the image saved", f)
	}
}

// A pass done with a host leaves it for the next look within seconds, not
// for the claim's two-minute lease. The recheck repeats no stop EC2 is
// still carrying out.
func TestAStoppingReserveIsRecheckedWithinSecondsOfItsStopCall(t *testing.T) {
	t.Parallel()
	o, emulator, ec2 := reserveFleet(t)
	host := reserve(t, o, compute.PhaseStopping, compute.MarketOnDemand, compute.ReserveHibernate, "i-0000000000000c030")
	ec2.add(fakeInstance{ID: "i-0000000000000c030", State: "running", Hibernation: true, Launched: time.Now().Add(-time.Hour)}, host.String())
	recheck := func() {
		run(t, o.pool, "update hosts set launch_lease_until = launch_lease_until - interval '10 seconds' where id = $1", uuid.UUID(host))
		actuate(t, o)
	}

	if r := actuate(t, o); r.Stopped != 1 {
		t.Fatalf("actuate %+v, want one hibernation", r)
	}
	recheck()
	if n := len(emulator.calls("StopInstances")); n != 1 {
		t.Fatalf("%d StopInstances calls, want the one while EC2 stops it", n)
	}
	ec2.set("i-0000000000000c030", func(i *fakeInstance) { i.State = "stopped" })
	recheck()
	if f := stopOf(t, o, host); f.Phase != "stopped" || f.Evidence != string(compute.EvidenceSaved) {
		t.Fatalf("after EC2 stopped it %+v, want stopped on the recheck with the image saved", f)
	}
}

// EC2 refuses to start a stopped Spot instance until it disables its
// persistent request, so the reserve waits in stopping until then, with
// its instance's stop recorded, and a resume asked meanwhile starts it
// once EC2 allows.
func TestASpotReserveStartsOnlyOnceEC2DisablesItsRequest(t *testing.T) {
	t.Parallel()
	o, emulator, ec2 := reserveFleet(t)
	host := reserve(t, o, compute.PhaseStopping, compute.MarketSpot, compute.ReserveStop, "i-0000000000000c040")
	ec2.add(fakeInstance{ID: "i-0000000000000c040", State: "stopped", SpotRequest: "sir-0000c040"}, host.String())
	ec2.addRequest(fakeSpotRequest{ID: "sir-0000c040", State: "active", Instance: "i-0000000000000c040"}, host.String())
	run(t, o.pool, "update hosts set spot_request_id = 'sir-0000c040', stop_requested_at = now() where id = $1", uuid.UUID(host))
	waiting := reserve(t, o, compute.PhaseStopping, compute.MarketSpot, compute.ReserveStop, "i-0000000000000c041")
	ec2.add(fakeInstance{ID: "i-0000000000000c041", State: "stopped", SpotRequest: "sir-0000c041"}, waiting.String())
	ec2.addRequest(fakeSpotRequest{ID: "sir-0000c041", State: "active", Instance: "i-0000000000000c041"}, waiting.String())
	run(t, o.pool, "update hosts set spot_request_id = 'sir-0000c041', stop_requested_at = now() where id = $1", uuid.UUID(waiting))

	actuate(t, o)
	for _, h := range []compute.HostID{host, waiting} {
		if f := stopOf(t, o, h); f.Phase != "stopping" || !f.Stopped {
			t.Fatalf("reserve with its Spot request still active %+v, want stopping with its instance's stop recorded", f)
		}
	}
	run(t, o.pool, "update hosts set phase = 'resuming' where id = $1", uuid.UUID(waiting))
	expireLeases(t, o)
	actuate(t, o)
	if n := len(emulator.calls("StartInstances")); n != 0 {
		t.Fatalf("%d StartInstances calls while EC2 holds the request active, want none", n)
	}
	ec2.addRequest(fakeSpotRequest{ID: "sir-0000c040", State: "disabled", Instance: "i-0000000000000c040"}, host.String())
	ec2.addRequest(fakeSpotRequest{ID: "sir-0000c041", State: "disabled", Instance: "i-0000000000000c041"}, waiting.String())
	expireLeases(t, o)
	if r := actuate(t, o); r.Started != 1 {
		t.Fatalf("actuate %+v once the requests disabled, want the resumed reserve started", r)
	}
	if f := stopOf(t, o, host); f.Phase != "stopped" {
		t.Fatalf("reserve with its Spot request disabled %+v, want stopped", f)
	}
}

// EC2 holds a stopped Spot reserve's request active for up to 165 s, and
// longer when it is slow. A resumed reserve waits for the request however
// long it stays active, and starts once EC2 disables it; reconcile ends a
// reserve that never resumes.
func TestAResumedSpotReserveWaitsForASlowDisable(t *testing.T) {
	t.Parallel()
	o, emulator, ec2 := reserveFleet(t)
	host := reserve(t, o, compute.PhaseResuming, compute.MarketSpot, compute.ReserveStop, "i-0000000000000c042")
	ec2.add(fakeInstance{ID: "i-0000000000000c042", State: "stopped", SpotRequest: "sir-0000c042"}, host.String())
	ec2.addRequest(fakeSpotRequest{ID: "sir-0000c042", State: "active", Instance: "i-0000000000000c042"}, host.String())
	run(t, o.pool, "update hosts set spot_request_id = 'sir-0000c042', stop_requested_at = now(), stopped_at = now() - interval '10 minutes' where id = $1", uuid.UUID(host))

	actuate(t, o)
	if phase, _ := hostPhase(t, o.pool, host); phase != string(compute.PhaseResuming) || len(emulator.calls("StartInstances")) != 0 {
		t.Fatalf("reserve whose request is active 10 minutes after the stop is %s after %d starts, want resuming without one",
			phase, len(emulator.calls("StartInstances")))
	}
	ec2.addRequest(fakeSpotRequest{ID: "sir-0000c042", State: "disabled", Instance: "i-0000000000000c042"}, host.String())
	expireLeases(t, o)
	if r := actuate(t, o); r.Started != 1 {
		t.Fatalf("actuate %+v once the request disabled, want the reserve started", r)
	}
}

// A reserve stopped for an hour waits out a failed read of its Spot
// request instead of taking it as stuck.
func TestAResumedSpotReserveWaitsOutAnUnreadRequest(t *testing.T) {
	t.Parallel()
	o, emulator, ec2 := reserveFleet(t)
	host := reserve(t, o, compute.PhaseResuming, compute.MarketSpot, compute.ReserveStop, "i-0000000000000c043")
	ec2.add(fakeInstance{ID: "i-0000000000000c043", State: "stopped", SpotRequest: "sir-0000c043"}, host.String())
	ec2.addRequest(fakeSpotRequest{ID: "sir-0000c043", State: "disabled", Instance: "i-0000000000000c043"}, host.String())
	run(t, o.pool, "update hosts set spot_request_id = 'sir-0000c043', stopped_at = now() - interval '1 hour' where id = $1", uuid.UUID(host))
	ec2.refuse = func(call awsCall) (awsReply, bool) {
		return ec2Error(http.StatusForbidden, "UnauthorizedOperation", "You are not authorized to perform this operation."),
			call.Action == "DescribeSpotInstanceRequests"
	}

	actuate(t, o)
	if phase, _ := hostPhase(t, o.pool, host); phase != string(compute.PhaseResuming) {
		t.Fatalf("reserve whose request went unread is %s, want resuming", phase)
	}
	ec2.refuse = nil
	expireLeases(t, o)
	if r := actuate(t, o); r.Started != 1 || len(emulator.calls("StartInstances")) != 1 {
		t.Fatalf("actuate %+v once the request reads disabled, want the reserve started", r)
	}
}

// EC2 names a hibernation in the stop reason; a hibernation that stopped
// for another reason saved no image.
func TestAHibernationStoppedForAnotherReasonSavedNoImage(t *testing.T) {
	t.Parallel()
	o, _, ec2 := reserveFleet(t)
	host := reserve(t, o, compute.PhaseStopping, compute.MarketOnDemand, compute.ReserveHibernate, "i-0000000000000c007")
	ec2.add(fakeInstance{ID: "i-0000000000000c007", State: "stopped", Hibernation: true, StateReason: "Client.InstanceInitiatedShutdown"}, host.String())
	run(t, o.pool, "update hosts set stop_requested_at = now(), image_evidence = 'unknown' where id = $1", uuid.UUID(host))
	plain := reserve(t, o, compute.PhaseStopping, compute.MarketOnDemand, compute.ReserveStop, "i-0000000000000c008")
	ec2.add(fakeInstance{ID: "i-0000000000000c008", State: "stopped", StateReason: "Client.UserInitiatedShutdown"}, plain.String())
	run(t, o.pool, "update hosts set stop_requested_at = now(), image_evidence = 'unavailable' where id = $1", uuid.UUID(plain))
	actuate(t, o)
	if f := stopOf(t, o, host); f.Phase != "stopped" || f.Evidence != string(compute.EvidenceFailed) {
		t.Fatalf("hibernation stopped otherwise %+v, want stopped with the image failed", f)
	}
	if f := stopOf(t, o, plain); f.Phase != "stopped" || f.Evidence != string(compute.EvidenceUnavailable) {
		t.Fatalf("plain stop %+v, want stopped with no image", f)
	}
}

func TestHibernationRefusalsRetryForTenMinutesThenTheReserveStopsPlainly(t *testing.T) {
	t.Parallel()
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
	t.Parallel()
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
	t.Parallel()
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
	// Both answers lost: EC2 stopped it plainly before the actuator saw
	// the stop. The last stop's saved image says nothing about this one.
	unseen := reserve(t, o, compute.PhaseStopping, compute.MarketOnDemand, compute.ReserveHibernate, "i-0000000000000c009")
	ec2.add(fakeInstance{ID: "i-0000000000000c009", State: "stopped", Hibernation: true, StateReason: "Client.InstanceInitiatedShutdown"}, unseen.String())
	run(t, o.pool, "update hosts set image_evidence = 'saved' where id = $1", uuid.UUID(unseen))
	expireLeases(t, o)
	actuate(t, o)
	if n := len(emulator.calls("StopInstances")); n != 1 {
		t.Fatalf("%d StopInstances calls, want no repeat inside the forced stop's window", n)
	}
	if f := stopOf(t, o, unseen); f.Phase != "stopped" || f.Evidence != string(compute.EvidenceFailed) {
		t.Fatalf("host stopped unseen %+v, want stopped with this stop's evidence", f)
	}
}

// A resumed reserve starts once, and a start EC2 refuses for capacity
// leaves it stopped in the reserve with its zone cooled.
func TestResumeStartsTheReserveOnceAndARefusedStartKeepsIt(t *testing.T) {
	t.Parallel()
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
	if f := stopOf(t, o, spot); f.Phase != "stopped" {
		t.Fatalf("reserve whose start EC2 refused %+v, want stopped in the reserve", f)
	}
	cooled := scan[string](t, o.pool, `select region || '/' || availability_zone_id || '/' || instance_type || '/' || market
from capacity_cooldowns where until > now()`)
	want := scan[string](t, o.pool, `select region || '/' || coalesce(availability_zone_id, '') || '/' || instance_type || '/' || market
from hosts where id = $1`, uuid.UUID(spot))
	if cooled != want {
		t.Fatalf("cooldown %q, want the refused zone %q", cooled, want)
	}
	if n := len(emulator.calls("TerminateInstances")); n != 0 {
		t.Fatalf("%d terminations after a refused start, want none", n)
	}
}

// A persistent request whose instance was replaced while the fleet was not
// looking ends with every instance it launched.
func TestTerminatingASpotReserveEndsEveryInstanceItsRequestLaunched(t *testing.T) {
	t.Parallel()
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
	t.Parallel()
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
		stopped: compute.PhaseStopped, settling: compute.PhaseStopping, preparing: compute.PhaseFailed,
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

// A serving Spot host's one-time request ends with its instance; the
// platform may not cancel it, so termination must not try.
func TestServingSpotHostsTerminateWithoutTheirOneTimeRequest(t *testing.T) {
	t.Parallel()
	o, emulator, ec2 := reserveFleet(t)
	host := scan[uuid.UUID](t, o.pool, `
insert into hosts (name, state, kind, provider, phase, cpu_millis, memory_bytes, region, instance_type, market)
values ('s', 'offline', 'platform', 'aws', 'requested', 1500, 6::bigint << 30, 'us-east-2', 'm7i.large', 'spot') returning id`)
	if n := launch(t, o); n != 1 {
		t.Fatalf("launched %d, want 1", n)
	}
	instance := scan[string](t, o.pool, "select instance_id from hosts where id = $1", host)
	if ec2.get(instance).SpotRequest == "" {
		t.Fatal("the fake launched a Spot instance without a request")
	}
	if request := scan[*string](t, o.pool, "select spot_request_id from hosts where id = $1", host); request != nil {
		t.Fatalf("serving host recorded one-time request %s", *request)
	}
	run(t, o.pool, "update hosts set phase = 'terminating' where id = $1", host)
	if r := actuate(t, o); r.Terminated != 1 {
		t.Fatalf("actuate %+v, want the host terminated", r)
	}
	if s := ec2.get(instance).State; s != "shutting-down" || len(emulator.calls("CancelSpotInstanceRequests")) != 0 {
		t.Fatalf("instance %s after %d cancels, want shutting down with no cancel", s, len(emulator.calls("CancelSpotInstanceRequests")))
	}
}

func TestAStopThatCannotHappenRetiresTheHostInsteadOfWaiting(t *testing.T) {
	t.Parallel()
	o, emulator, ec2 := reserveFleet(t)
	// A serving one-time Spot host sent to the reserve cannot stop.
	oneTime := reserve(t, o, compute.PhaseStopping, compute.MarketSpot, compute.ReserveStop, "i-0000000000000c010")
	ec2.add(fakeInstance{ID: "i-0000000000000c010", State: "running", SpotRequest: "sir-0000c010"}, oneTime.String())
	refused := reserve(t, o, compute.PhaseStopping, compute.MarketOnDemand, compute.ReserveStop, "i-0000000000000c011")
	ec2.add(fakeInstance{ID: "i-0000000000000c011", State: "running"}, refused.String())
	throttled := reserve(t, o, compute.PhaseStopping, compute.MarketOnDemand, compute.ReserveStop, "i-0000000000000c012")
	ec2.add(fakeInstance{ID: "i-0000000000000c012", State: "running"}, throttled.String())
	ec2.refuse = func(call awsCall) (awsReply, bool) {
		switch {
		case call.Action != "StopInstances":
			return awsReply{}, false
		case call.Form.Get("InstanceId.1") == "i-0000000000000c011":
			return ec2Error(http.StatusBadRequest, "UnsupportedOperation", "The instance does not support stopping"), true
		default:
			return ec2Error(http.StatusServiceUnavailable, "RequestLimitExceeded", "Request limit exceeded."), true
		}
	}
	actuate(t, o)
	for host, want := range map[compute.HostID]compute.Phase{
		oneTime: compute.PhaseTerminating, refused: compute.PhaseTerminating, throttled: compute.PhaseStopping,
	} {
		if phase, _ := hostPhase(t, o.pool, host); phase != string(want) {
			t.Errorf("host %s is %s, want %s", host, phase, want)
		}
	}
	for _, c := range emulator.calls("StopInstances") {
		if c.Form.Get("InstanceId.1") == "i-0000000000000c010" {
			t.Error("asked EC2 to stop a one-time Spot instance")
		}
	}

	// A stop that never completes fails once its window passes.
	ago(t, o, throttled, "phase_at", 25*time.Minute)
	if err := o.compute.Reconcile(t.Context(), discard()); err != nil {
		t.Fatal(err)
	}
	if phase, _ := hostPhase(t, o.pool, throttled); phase != string(compute.PhaseFailed) {
		t.Fatalf("host stopping for 25 minutes is %s, want failed", phase)
	}
	if s := ec2.get("i-0000000000000c012").State; s != "shutting-down" {
		t.Fatalf("its instance is %s, want shutting down", s)
	}
}

// A stop EC2 refuses outright, as under a role without ec2:StopInstances,
// retires the host and cools its offer, so the next reserve pass buys the
// reserve elsewhere instead of a host that would be refused the same way.
func TestARefusedStopCoolsItsOfferSoTheNextPassBuysElsewhere(t *testing.T) {
	t.Parallel()
	o, _, ec2 := reserveFleet(t)
	host := reserve(t, o, compute.PhaseStopping, compute.MarketOnDemand, compute.ReserveStop, "i-0000000000000c021")
	run(t, o.pool, "update hosts set instance_type = 'c6a.2xlarge' where id = $1", uuid.UUID(host))
	ec2.add(fakeInstance{ID: "i-0000000000000c021", State: "running", Type: "c6a.2xlarge"}, host.String())
	ec2.refuse = func(call awsCall) (awsReply, bool) {
		return ec2Error(http.StatusForbidden, "UnauthorizedOperation", "You are not authorized to perform this operation."),
			call.Action == "StopInstances"
	}
	actuate(t, o)
	if phase, _ := hostPhase(t, o.pool, host); phase != string(compute.PhaseTerminating) {
		t.Fatalf("host whose stop was refused is %s, want terminating", phase)
	}
	cooled := scan[bool](t, o.pool, `select exists (select 1 from capacity_cooldowns where connection_key = 'platform'
and region = 'us-east-2' and instance_type = 'c6a.2xlarge' and market = 'on_demand' and until > now())`)
	if !cooled {
		t.Fatal("the refused host's offer did not cool")
	}
	if r := plan(t, o); r.Requested == 0 {
		t.Fatalf("reserve pass %+v, want the reserve bought", r)
	}
	if n := scan[int](t, o.pool, `select count(*) from hosts where phase = 'requested' and instance_type = 'c6a.2xlarge'
and region = 'us-east-2' and market = 'on_demand'`); n != 0 {
		t.Fatalf("bought %d more of the refused offer", n)
	}
}

// Reconcile reads hosts, then EC2. A host the session or actuator moves in
// between keeps its new phase and its instance.
func TestReconcileLeavesAHostThatMovedSinceItsSnapshot(t *testing.T) {
	t.Parallel()
	o, _, ec2 := reserveFleet(t)
	stopping := reserve(t, o, compute.PhasePreparing, compute.MarketOnDemand, compute.ReserveStop, "i-0000000000000e010")
	ec2.add(fakeInstance{ID: "i-0000000000000e010", State: "running"}, stopping.String())
	joined := reserve(t, o, compute.PhaseResuming, compute.MarketOnDemand, compute.ReserveStop, "i-0000000000000e011")
	ec2.add(fakeInstance{ID: "i-0000000000000e011", State: "running"}, joined.String())
	ago(t, o, joined, "phase_at", 25*time.Minute)
	moved := false
	ec2.refuse = func(call awsCall) (awsReply, bool) {
		if call.Action == "DescribeInstances" && !moved {
			moved = true
			// The session proved one and the actuator stopped it; the
			// other's agent said Hello.
			run(t, o.pool, "update hosts set phase = 'stopping', phase_at = now(), stop_requested_at = now() where id = $1", uuid.UUID(stopping))
			run(t, o.pool, "update hosts set phase = 'joining', phase_at = now() where id = $1", uuid.UUID(joined))
			ec2.instances["i-0000000000000e010"].State = "stopping"
		}
		return awsReply{}, false
	}
	if err := o.compute.Reconcile(t.Context(), discard()); err != nil {
		t.Fatal(err)
	}
	for host, want := range map[compute.HostID]compute.Phase{stopping: compute.PhaseStopping, joined: compute.PhaseJoining} {
		if phase, _ := hostPhase(t, o.pool, host); phase != string(want) {
			t.Errorf("host %s is %s, want %s", host, phase, want)
		}
	}
	for id, want := range map[string]string{"i-0000000000000e010": "stopping", "i-0000000000000e011": "running"} {
		if s := ec2.get(id).State; s != want {
			t.Errorf("instance %s is %s, want %s", id, s, want)
		}
	}
}

// A reserve whose start EC2 refused waits out the capacity cooldown, even
// in a pool a launch walk refused first, so the work it was resumed for
// buys a host on the next pass rather than resuming it again.
func TestARefusedStartLetsItsWorkBuy(t *testing.T) {
	t.Parallel()
	o, _, ec2 := reserveFleet(t)
	host := stoppedReserve(t, o, "m7i.xlarge", "i-0000000000000d101")
	ec2.add(fakeInstance{ID: "i-0000000000000d101", State: "stopped", Type: "m7i.xlarge"}, host.String())
	run(t, o.pool, `insert into capacity_cooldowns (connection_key, region, availability_zone_id, instance_type, market, until, cooled_for, reason, walk)
values ('platform', 'us-east-2', 'use2-az1', 'm7i.xlarge', 'on_demand', now() + interval '10 minutes', interval '10 minutes', 'refused', gen_random_uuid())`)
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{"placement": {"preemptible": false}}`), 1000, gib)
	if r := planCapacity(t, o); r.Resumed != 1 {
		t.Fatalf("plan %+v, want the reserve resumed", r)
	}
	ec2.refuse = func(call awsCall) (awsReply, bool) {
		return ec2Error(http.StatusInternalServerError, "InsufficientInstanceCapacity", "We currently do not have sufficient m7i.xlarge capacity"),
			call.Action == "StartInstances"
	}
	actuate(t, o)
	if phase, _ := hostPhase(t, o.pool, host); phase != string(compute.PhaseStopped) {
		t.Fatalf("refused reserve is %s, want stopped", phase)
	}
	if r := planCapacity(t, o); r.Resumed != 0 || r.Requested != 1 {
		t.Fatalf("plan after the refused start %+v, want a host bought and no resume", r)
	}
}

// A terminating host whose instance EC2 is shutting down is ending
// already: the actuator asks EC2 to terminate it no more.
func TestAShuttingDownInstanceIsNotTerminatedAgain(t *testing.T) {
	t.Parallel()
	o, emulator, ec2 := reserveFleet(t)
	host := reserve(t, o, compute.PhaseTerminating, compute.MarketOnDemand, compute.ReserveStop, "i-0000000000000d201")
	ec2.add(fakeInstance{ID: "i-0000000000000d201", State: "shutting-down"}, host.String())
	for range 2 {
		actuate(t, o)
		expireLeases(t, o)
	}
	if n := len(emulator.calls("TerminateInstances")); n != 0 {
		t.Fatalf("%d TerminateInstances calls on a shutting-down instance, want none", n)
	}
}

// A Spot GPU host bought for work runs on a persistent request that stops
// on interruption, so it can return to the reserve when it leaves; a Spot
// CPU host stays on a one-time request.
func TestSpotGPUWorkHostsLaunchOnPersistentRequests(t *testing.T) {
	t.Parallel()
	o, emulator, ec2 := reserveFleet(t)
	insert := func(instanceType string, gpus int) compute.HostID {
		return compute.HostID(scan[uuid.UUID](t, o.pool, `
insert into hosts (name, state, kind, provider, phase, cpu_millis, memory_bytes, region, instance_type, market, gpu_type, gpu_count)
values ('w', 'offline', 'platform', 'aws', 'requested', 2000, 8::bigint << 30, 'us-east-2', $1, 'spot', $2, $3) returning id`,
			instanceType, map[int]string{0: "", 1: "T4"}[gpus], gpus))
	}
	gpu, cpuHost := insert("g4dn.xlarge", 1), insert("m7i.large", 0)
	if n := launch(t, o); n != 2 {
		t.Fatalf("launched %d, want 2", n)
	}
	calls := map[string]awsCall{}
	for _, c := range emulator.calls("RunInstances") {
		calls[c.Form.Get("ClientToken")] = c
	}
	if f := calls[gpu.String()].Form; f.Get("InstanceMarketOptions.SpotOptions.SpotInstanceType") != "persistent" ||
		f.Get("InstanceMarketOptions.SpotOptions.InstanceInterruptionBehavior") != "stop" || f.Get("InstanceInitiatedShutdownBehavior") != "stop" {
		t.Errorf("Spot GPU work launch %v, want a persistent request that stops", f)
	}
	if f := calls[cpuHost.String()].Form; f.Get("InstanceMarketOptions.SpotOptions.SpotInstanceType") != "one-time" {
		t.Errorf("Spot CPU work launch %v, want a one-time request", f)
	}
	request := scan[*string](t, o.pool, "select spot_request_id from hosts where id = $1", uuid.UUID(gpu))
	if instance := scan[string](t, o.pool, "select instance_id from hosts where id = $1", uuid.UUID(gpu)); request == nil || *request != ec2.get(instance).SpotRequest {
		t.Fatalf("Spot GPU work host recorded request %v, want its persistent request", request)
	}
}
