package compute_test

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// acceptanceFleet is the tag the disposable account allows launches under.
const acceptanceFleet = "acceptance"

// TestRealEC2LaunchesAndTerminatesOneInstance launches one t3.micro in
// us-east-2 through the launcher, sees it run through reconciliation and
// terminates it through retirement. It talks to AWS itself, so it runs only
// with LAZYCLOUD_EC2_ACCEPTANCE_PROFILE naming a disposable profile; every
// instance tagged for the acceptance fleet is terminated when it ends.
func TestRealEC2LaunchesAndTerminatesOneInstance(t *testing.T) {
	profile := os.Getenv("LAZYCLOUD_EC2_ACCEPTANCE_PROFILE")
	if profile == "" {
		t.Skip("set LAZYCLOUD_EC2_ACCEPTANCE_PROFILE to a disposable AWS profile")
	}
	market := os.Getenv("LAZYCLOUD_EC2_ACCEPTANCE_MARKET")
	if market == "" {
		market = string(compute.MarketOnDemand)
	}
	ctx := t.Context()
	cfg, err := config.LoadDefaultConfig(ctx, config.WithSharedConfigProfile(profile), config.WithRegion("us-east-2"))
	if err != nil {
		t.Fatal(err)
	}
	client := ec2.NewFromConfig(cfg)
	t.Cleanup(func() { sweepAcceptance(t, client) })

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
	if len(network.Subnets) == 0 {
		t.Fatal("the account has no default subnet in us-east-2")
	}
	o := newOwners(t, compute.Config{
		InstallURL: "https://lazycloud.invalid", ServerAddress: "lazycloud.invalid:443",
		Fleet: compute.Fleet{Name: acceptanceFleet, AWS: cfg, MaxHosts: 1, Networks: map[string]compute.Network{"us-east-2": network}},
	})
	publish(t, o.compute)
	host := scan[uuid.UUID](t, o.pool, `
insert into hosts (name, state, kind, provider, phase, cpu_millis, memory_bytes, region, instance_type, market)
values ('acceptance', 'offline', 'platform', 'aws', 'requested', 1000, 1 << 30, 'us-east-2', 't3.micro', $1)
returning id`, market)
	phase := func() string {
		return scan[string](t, o.pool, "select phase from hosts where id = $1", host)
	}

	start := time.Now()
	if n, err := o.compute.Launch(ctx, discard()); err != nil || n != 1 {
		t.Fatalf("launch: %d %v; host %s: %s", n, err, phase(),
			scan[string](t, o.pool, "select phase_message from hosts where id = $1", host))
	}
	instance := scan[string](t, o.pool, "select instance_id from hosts where id = $1", host)
	t.Logf("RunInstances (%s) returned %s in %s", market, instance, time.Since(start).Round(time.Millisecond))

	waitFor(t, o, "running", 3*time.Minute, func() bool { return phase() == string(compute.PhaseBooting) })
	t.Logf("instance running %s after launch", time.Since(start).Round(time.Second))

	run(t, o.pool, "update hosts set phase = 'draining' where id = $1", host)
	terminated := time.Now()
	if result, err := o.compute.Retire(ctx, discard()); err != nil || result.Terminated != 1 {
		t.Fatalf("retire: %+v %v", result, err)
	}
	waitFor(t, o, "gone", 3*time.Minute, func() bool { return phase() == string(compute.PhaseDeleted) })
	t.Logf("instance gone %s after TerminateInstances", time.Since(terminated).Round(time.Second))
}

func waitFor(t *testing.T, o owners, what string, limit time.Duration, done func() bool) {
	t.Helper()
	deadline := time.Now().Add(limit)
	for !done() {
		if time.Now().After(deadline) {
			t.Fatalf("instance not %s after %s", what, limit)
		}
		time.Sleep(5 * time.Second)
		if err := o.compute.Reconcile(t.Context(), discard()); err != nil {
			t.Logf("reconcile: %v", err)
		}
	}
}

// sweepAcceptance terminates every live acceptance instance and checks none
// is left running.
func sweepAcceptance(t *testing.T, client *ec2.Client) {
	ctx := context.WithoutCancel(t.Context())
	live := func() []string {
		out, err := client.DescribeInstances(ctx, &ec2.DescribeInstancesInput{Filters: []ec2types.Filter{
			{Name: aws.String("tag:lazycloud:fleet"), Values: []string{acceptanceFleet}},
			{Name: aws.String("instance-state-name"), Values: []string{"pending", "running", "stopping", "stopped"}},
		}})
		if err != nil {
			t.Errorf("sweep: describe instances: %v", err)
			return nil
		}
		var ids []string
		for _, r := range out.Reservations {
			for _, i := range r.Instances {
				ids = append(ids, aws.ToString(i.InstanceId))
			}
		}
		return ids
	}
	if ids := live(); len(ids) > 0 {
		t.Logf("sweep: terminating %v", ids)
		if _, err := client.TerminateInstances(ctx, &ec2.TerminateInstancesInput{InstanceIds: ids}); err != nil {
			t.Errorf("sweep: terminate %v: %v", ids, err)
		}
	}
	if ids := live(); len(ids) > 0 {
		t.Errorf("acceptance instances still live after the sweep: %v", ids)
	}
}
