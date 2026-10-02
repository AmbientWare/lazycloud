package compute_test

import (
	"os"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// TestRealEC2PlansPurchasesAndRetiresTheSurplus drives the planning pass
// against real EC2 in us-east-2: with live Spot prices it buys a Spot
// m7i.xlarge-class host in each of two zones for two zone-pinned
// containers, the launcher starts the planned type in the planned zone and
// market, and once the market is quiet the pass drains the host the warm
// target can spare and retirement terminates it, its one-time Spot request
// closing with it. The hosts cannot enroll from the disposable account, so
// the test stands in for their sessions. It runs only with
// LAZYCLOUD_EC2_ACCEPTANCE_PROFILE naming a disposable profile; every
// acceptance instance and Spot request is swept when it ends.
func TestRealEC2PlansPurchasesAndRetiresTheSurplus(t *testing.T) {
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
	if len(network.Subnets) < 2 {
		t.Fatalf("the account has %d default subnets in us-east-2, want two zones", len(network.Subnets))
	}
	o := newOwners(t, compute.Config{
		InstallURL: "https://lazycloud.invalid", ServerAddress: "lazycloud.invalid:443",
		Fleet: compute.Fleet{Name: acceptanceFleet, AWS: cfg, MaxHosts: 2, Networks: map[string]compute.Network{"us-east-2": network}},
	})
	publish(t, o.compute)
	if _, err := o.compute.RefreshSpotPrices(ctx, discard()); err != nil {
		t.Fatalf("refresh spot prices: %v", err)
	}
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	var containers []uuid.UUID
	for _, s := range network.Subnets[:2] {
		release := newRelease(t, o.pool, dev, `{"placement": {"availability_zone": "`+s.ZoneID+`"}}`)
		containers = append(containers, pendingContainer(t, o.pool, dev, release, 3000, 4*gib))
	}
	run(t, o.pool, `insert into fleet_markets (market, plan, generated_at, expires_at) values ('spot:cpu', '{}', now(), now() + interval '5 minutes')`)
	if r := plan(t, o); r.Requested != 2 {
		t.Fatalf("plan %+v, want a host per zone", r)
	}
	if n := launch(t, o); n != 2 {
		t.Fatalf("launched %d, want both planned hosts", n)
	}
	rows, err := o.pool.Query(ctx, `select id, instance_id, instance_type, market, availability_zone from hosts where phase = 'provisioning'`)
	if err != nil {
		t.Fatal(err)
	}
	type bought struct {
		id                          uuid.UUID
		instance, typ, market, zone string
	}
	var hosts []bought
	for rows.Next() {
		var b bought
		if err := rows.Scan(&b.id, &b.instance, &b.typ, &b.market, &b.zone); err != nil {
			t.Fatal(err)
		}
		hosts = append(hosts, b)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	for _, h := range hosts {
		out, err := client.DescribeInstances(ctx, &ec2.DescribeInstancesInput{InstanceIds: []string{h.instance}})
		if err != nil || len(out.Reservations) != 1 {
			t.Fatalf("describe %s: %v", h.instance, err)
		}
		i := out.Reservations[0].Instances[0]
		spot := i.InstanceLifecycle == ec2types.InstanceLifecycleTypeSpot
		if string(i.InstanceType) != h.typ || aws.ToString(i.Placement.AvailabilityZone) != h.zone || spot != (h.market == string(compute.MarketSpot)) {
			t.Fatalf("instance %s is %s %s spot=%t, want the planned %s %s %s", h.instance, i.InstanceType,
				aws.ToString(i.Placement.AvailabilityZone), spot, h.typ, h.zone, h.market)
		}
	}

	// Both hosts serve and go quiet.
	run(t, o.pool, "update containers set state = 'stopped', stop_reason = 'stopped', stopped_at = now() where id = any($1)", containers)
	run(t, o.pool, `update hosts set phase = 'ready', state = 'online', last_seen_at = now(), token_hash = sha256(id::text::bytea),
	                launched_at = now() - interval '1 hour', light_since = now() - interval '1 hour' where phase = 'provisioning'`)
	run(t, o.pool, "update fleet_markets set generated_at = now() - interval '61 seconds'")
	if r := plan(t, o); r.Drained != 1 {
		t.Fatalf("quiet pass %+v, want the host the warm target spares drained", r)
	}
	if r, err := o.compute.Retire(ctx, discard()); err != nil || r.Terminated != 1 {
		t.Fatalf("retire %+v %v, want the drained host terminated", r, err)
	}
	terminating := scan[string](t, o.pool, "select instance_id from hosts where phase = 'terminating'")
	out, err := client.DescribeInstances(ctx, &ec2.DescribeInstancesInput{InstanceIds: []string{terminating}})
	if err != nil || len(out.Reservations) != 1 {
		t.Fatalf("describe %s: %v", terminating, err)
	}
	i := out.Reservations[0].Instances[0]
	if s := i.State.Name; s != ec2types.InstanceStateNameShuttingDown && s != ec2types.InstanceStateNameTerminated {
		t.Fatalf("retired instance is %s", s)
	}
	if request := aws.ToString(i.SpotInstanceRequestId); request != "" {
		requests, err := client.DescribeSpotInstanceRequests(ctx, &ec2.DescribeSpotInstanceRequestsInput{SpotInstanceRequestIds: []string{request}})
		if err != nil || len(requests.SpotInstanceRequests) != 1 {
			t.Fatalf("describe spot request %s: %v", request, err)
		}
		if s := requests.SpotInstanceRequests[0].State; s == ec2types.SpotInstanceStateOpen {
			t.Fatalf("spot request %s is %s after its instance retired", request, s)
		}
	}
}
