package compute_test

import (
	"context"
	"encoding/base64"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// TestRealEC2ReservesHibernateStopStartAndRetire drives two m7i.large
// reserves in us-east-2 through the launcher, the actuator and
// reconciliation: an on-demand one that hibernates and starts again, and a
// Spot one on a persistent request that stops, starts and terminates with
// its request cancelled. The agent cannot reach a server from the
// disposable account, so the test stands in for the host session's phase
// moves. It runs only with LAZYCLOUD_EC2_ACCEPTANCE_PROFILE naming a
// disposable profile; every acceptance instance and Spot request is swept
// when it ends.
func TestRealEC2ReservesHibernateStopStartAndRetire(t *testing.T) {
	profile := os.Getenv("LAZYCLOUD_EC2_ACCEPTANCE_PROFILE")
	if profile == "" {
		t.Skip("set LAZYCLOUD_EC2_ACCEPTANCE_PROFILE to a disposable AWS profile")
	}
	ctx := t.Context()
	cfg, err := config.LoadDefaultConfig(ctx, config.WithSharedConfigProfile(profile), config.WithRegion("us-east-2"))
	if err != nil {
		t.Fatal(err)
	}
	client := ec2.NewFromConfig(cfg)
	t.Cleanup(func() {
		sweepAcceptanceSpotRequests(t, client)
		sweepAcceptance(t, client)
	})
	subnets, err := client.DescribeSubnets(ctx, &ec2.DescribeSubnetsInput{
		Filters: []ec2types.Filter{{Name: aws.String("default-for-az"), Values: []string{"true"}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	network := compute.Network{}
	for _, s := range subnets.Subnets {
		network.VPCID = aws.ToString(s.VpcId)
		network.Subnets = append(network.Subnets, compute.Subnet{
			ID: aws.ToString(s.SubnetId), Zone: aws.ToString(s.AvailabilityZone), ZoneID: aws.ToString(s.AvailabilityZoneId),
		})
	}
	o := newOwners(t, compute.Config{
		InstallURL: "https://lazycloud.invalid", ServerAddress: "lazycloud.invalid:443",
		Fleet: compute.Fleet{Name: acceptanceFleet, AWS: cfg, MaxHosts: 2, Networks: map[string]compute.Network{"us-east-2": network}},
	})
	publish(t, o.compute)

	for _, c := range []struct {
		market compute.Market
		mode   compute.ReserveMode
	}{{compute.MarketOnDemand, compute.ReserveHibernate}, {compute.MarketSpot, compute.ReserveStop}} {
		t.Run(string(c.market)+"_"+string(c.mode), func(t *testing.T) {
			cycleReserve(t, o, client, c.market, c.mode)
		})
	}
}

func cycleReserve(t *testing.T, o owners, client *ec2.Client, market compute.Market, mode compute.ReserveMode) {
	host := scan[uuid.UUID](t, o.pool, `
insert into hosts (name, state, kind, provider, phase, cpu_millis, memory_bytes, region, instance_type, market, reserve_mode)
values ('acceptance', 'offline', 'platform', 'aws', 'requested', 1500, 6::bigint << 30, 'us-east-2', 'm7i.large', $1, $2)
returning id`, string(market), string(mode))
	phase := func() string { return scan[string](t, o.pool, "select phase from hosts where id = $1", host) }
	move := func(to compute.Phase) {
		run(t, o.pool, "update hosts set phase = $2, phase_at = now(), launch_lease_until = null where id = $1", host, string(to))
	}
	act := func() {
		run(t, o.pool, "update hosts set launch_lease_until = null where id = $1", host)
		if _, err := o.compute.Actuate(t.Context(), discard()); err != nil {
			t.Logf("actuate: %v", err)
		}
	}
	describe := func() ec2types.Instance {
		instance := scan[string](t, o.pool, "select instance_id from hosts where id = $1", host)
		out, err := client.DescribeInstances(t.Context(), &ec2.DescribeInstancesInput{InstanceIds: []string{instance}})
		if err != nil || len(out.Reservations) != 1 {
			t.Fatalf("describe %s: %v", instance, err)
		}
		return out.Reservations[0].Instances[0]
	}
	until := func(what string, limit time.Duration, step func(), done func() bool) time.Duration {
		start := time.Now()
		for !done() {
			if time.Since(start) > limit {
				t.Fatalf("not %s after %s; host %s", what, limit, phase())
			}
			time.Sleep(5 * time.Second)
			step()
		}
		return time.Since(start).Round(time.Second)
	}
	reconcile := func() {
		if err := o.compute.Reconcile(t.Context(), discard()); err != nil {
			t.Logf("reconcile: %v", err)
		}
	}

	start := time.Now()
	if n, err := o.compute.Launch(t.Context(), discard()); err != nil || n != 1 {
		t.Fatalf("launch: %d %v; %s", n, err, scan[string](t, o.pool, "select phase_message from hosts where id = $1", host))
	}
	var request *string
	var configured bool
	if err := o.pool.QueryRow(t.Context(), "select spot_request_id, hibernation_configured from hosts where id = $1", host).Scan(&request, &configured); err != nil {
		t.Fatal(err)
	}
	t.Logf("launched %s, spot request %v, hibernation %t, in %s", aws.ToString(describe().InstanceId), deref(request), configured,
		time.Since(start).Round(time.Millisecond))
	if (market == compute.MarketSpot) != (request != nil) || configured != (mode == compute.ReserveHibernate) {
		t.Fatalf("recorded request %v and hibernation %t for a %s %s reserve", request, configured, market, mode)
	}
	if request != nil {
		out, err := client.DescribeSpotInstanceRequests(t.Context(), &ec2.DescribeSpotInstanceRequestsInput{SpotInstanceRequestIds: []string{*request}})
		if err != nil || len(out.SpotInstanceRequests) != 1 || out.SpotInstanceRequests[0].Type != ec2types.SpotInstanceTypePersistent {
			t.Fatalf("spot request %v: %v", out, err)
		}
		r := out.SpotInstanceRequests[0]
		t.Logf("spot request %s %s, interruption %s, tags %d", *request, r.Type, r.InstanceInterruptionBehavior, len(r.Tags))
	}
	running := until("running", 5*time.Minute, reconcile, func() bool { return phase() == string(compute.PhaseBooting) })
	t.Logf("running %s after launch", running)

	// The session would move a proven host to stopping.
	move(compute.PhaseStopping)
	stopAsked := time.Now()
	accepted := until("stop accepted", 15*time.Minute, act, func() bool {
		return scan[bool](t, o.pool, "select stop_requested_at is not null from hosts where id = $1", host)
	})
	evidence := scan[string](t, o.pool, "select image_evidence from hosts where id = $1", host)
	t.Logf("stop accepted %s after stopping began (%s)", accepted, evidence)
	stopped := until("stopped", 15*time.Minute, act, func() bool { return phase() == string(compute.PhaseStopped) })
	i := describe()
	evidence = scan[string](t, o.pool, "select image_evidence from hosts where id = $1", host)
	t.Logf("stopped %s after the stop was accepted; reason %s; evidence %s; whole stop %s",
		stopped, aws.ToString(i.StateReason.Code), evidence, time.Since(stopAsked).Round(time.Second))
	if mode == compute.ReserveHibernate && evidence != string(compute.EvidenceSaved) {
		t.Errorf("hibernated reserve evidence %s, want saved", evidence)
	}

	// The planner would resume it.
	run(t, o.pool, "update hosts set resume_requested_at = now() where id = $1", host)
	move(compute.PhaseResuming)
	act()
	started := until("running again", 10*time.Minute, func() {}, func() bool {
		return describe().State.Name == ec2types.InstanceStateNameRunning
	})
	t.Logf("running %s after the start was asked", started)
	if mode == compute.ReserveHibernate {
		time.Sleep(30 * time.Second)
		instance := aws.ToString(describe().InstanceId)
		console, err := client.GetConsoleOutput(t.Context(), &ec2.GetConsoleOutputInput{InstanceId: aws.String(instance), Latest: aws.Bool(true)})
		if err != nil {
			t.Logf("console: %v", err)
		} else if text, err := decodeConsole(aws.ToString(console.Output)); err == nil {
			for _, line := range strings.Split(text, "\n") {
				if strings.Contains(line, "PM: ") || strings.Contains(line, "hibernat") {
					t.Logf("console: %s", strings.TrimSpace(line))
				}
			}
		}
	}

	// The planner would retire it.
	move(compute.PhaseTerminating)
	act()
	gone := until("gone", 5*time.Minute, reconcile, func() bool { return phase() == string(compute.PhaseDeleted) })
	t.Logf("deleted %s after the terminate", gone)
	if request != nil {
		out, err := client.DescribeSpotInstanceRequests(t.Context(), &ec2.DescribeSpotInstanceRequestsInput{SpotInstanceRequestIds: []string{*request}})
		if err != nil || len(out.SpotInstanceRequests) != 1 || out.SpotInstanceRequests[0].State != ec2types.SpotInstanceStateCancelled {
			t.Fatalf("spot request after retirement %v: %v, want cancelled", out, err)
		}
		launched, err := client.DescribeInstances(t.Context(), &ec2.DescribeInstancesInput{Filters: []ec2types.Filter{
			{Name: aws.String("spot-instance-request-id"), Values: []string{*request}},
			{Name: aws.String("instance-state-name"), Values: []string{"pending", "running", "stopping", "stopped"}},
		}})
		if err != nil || len(launched.Reservations) != 0 {
			t.Fatalf("instances of the cancelled request %v: %v, want none live", launched, err)
		}
	}
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func decodeConsole(s string) (string, error) {
	b, err := base64.StdEncoding.DecodeString(s)
	return string(b), err
}

// sweepAcceptanceSpotRequests cancels every acceptance Spot request still
// open, active or disabled.
func sweepAcceptanceSpotRequests(t *testing.T, client *ec2.Client) {
	ctx := context.WithoutCancel(t.Context())
	out, err := client.DescribeSpotInstanceRequests(ctx, &ec2.DescribeSpotInstanceRequestsInput{Filters: []ec2types.Filter{
		{Name: aws.String("tag:lazycloud:fleet"), Values: []string{acceptanceFleet}},
		{Name: aws.String("state"), Values: []string{"open", "active", "disabled"}},
	}})
	if err != nil {
		t.Errorf("sweep: describe spot requests: %v", err)
		return
	}
	var ids []string
	for _, r := range out.SpotInstanceRequests {
		ids = append(ids, aws.ToString(r.SpotInstanceRequestId))
	}
	if len(ids) == 0 {
		return
	}
	t.Logf("sweep: cancelling %v", ids)
	if _, err := client.CancelSpotInstanceRequests(ctx, &ec2.CancelSpotInstanceRequestsInput{SpotInstanceRequestIds: ids}); err != nil {
		t.Errorf("sweep: cancel %v: %v", ids, err)
	}
}
