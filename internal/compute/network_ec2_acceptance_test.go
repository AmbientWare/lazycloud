package compute_test

import (
	"cmp"
	"context"
	"os"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// TestRealEC2Network makes a fleet network in a real account, checks its
// shape, makes it again without creating anything new, and deletes it. It
// runs only with LAZYCLOUD_EC2_ACCEPTANCE_PROFILE naming an AWS profile, in
// acceptanceRegion(), under a fleet name of its own or
// LAZYCLOUD_EC2_ACCEPTANCE_FLEET, the one a connection role's tag conditions
// name; cleanup deletes whatever of the network is left.
func TestRealEC2Network(t *testing.T) {
	t.Parallel()
	profile := os.Getenv("LAZYCLOUD_EC2_ACCEPTANCE_PROFILE")
	if profile == "" {
		t.Skip("set LAZYCLOUD_EC2_ACCEPTANCE_PROFILE to an AWS profile")
	}
	region := acceptanceRegion()
	cfg, err := config.LoadDefaultConfig(t.Context(), config.WithSharedConfigProfile(profile), config.WithRegion(region))
	if err != nil {
		t.Fatal(err)
	}
	fleet := cmp.Or(os.Getenv("LAZYCLOUD_EC2_ACCEPTANCE_FLEET"), "acceptance-net-"+uuid.NewString()[:8])
	c := compute.NewCompute(nil, nil, compute.Config{Fleet: compute.Fleet{Name: fleet, AWS: cfg}})
	client := ec2.NewFromConfig(cfg)
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
		defer cancel()
		deleteUntilGone(ctx, t, c, region)
	})

	start := time.Now()
	network, err := c.EnsurePlatformNetwork(t.Context(), region)
	if err != nil {
		t.Fatalf("ensure: %v", err)
	}
	t.Logf("fleet %s: ensure made %+v in %s", fleet, network, time.Since(start).Round(time.Millisecond))
	checkNetworkShape(t, client, region, fleet, network)
	made := fleetResources(t, client, fleet)

	again, err := c.EnsurePlatformNetwork(t.Context(), region)
	if err != nil {
		t.Fatalf("ensure again: %v", err)
	}
	if !reflect.DeepEqual(again, network) {
		t.Fatalf("ensure again returned %+v, want %+v", again, network)
	}
	if after := fleetResources(t, client, fleet); !reflect.DeepEqual(after, made) {
		t.Fatalf("ensure again changed the fleet's resources from %v to %v", made, after)
	}

	start = time.Now()
	deleteUntilGone(t.Context(), t, c, region)
	t.Logf("delete took %s", time.Since(start).Round(time.Millisecond))
	if left := fleetResources(t, client, fleet); len(left) > 0 {
		t.Fatalf("delete left %v", left)
	}
}

// deleteUntilGone deletes the network until it is gone or ctx ends.
func deleteUntilGone(ctx context.Context, t *testing.T, c *compute.Compute, region string) {
	t.Helper()
	for attempt := 1; ; attempt++ {
		gone, err := c.DeletePlatformNetwork(ctx, region)
		if err != nil {
			t.Errorf("delete: %v", err)
			return
		}
		if gone {
			t.Logf("network gone after %d delete attempts", attempt)
			return
		}
		select {
		case <-ctx.Done():
			t.Errorf("network not gone: %v", ctx.Err())
			return
		case <-time.After(5 * time.Second):
		}
	}
}

// checkNetworkShape checks the network has a public subnet per available
// zone routed to an internet gateway, the node group's peer rule and egress,
// and the S3 endpoint on the route table.
func checkNetworkShape(t *testing.T, client *ec2.Client, region, fleet string, network compute.Network) {
	t.Helper()
	ctx := t.Context()
	zones, err := client.DescribeAvailabilityZones(ctx, &ec2.DescribeAvailabilityZonesInput{Filters: []ec2types.Filter{
		{Name: aws.String("zone-type"), Values: []string{"availability-zone"}},
		{Name: aws.String("state"), Values: []string{"available"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if len(network.Subnets) != len(zones.AvailabilityZones) {
		t.Fatalf("%d subnets for %d zones", len(network.Subnets), len(zones.AvailabilityZones))
	}
	inVPC := []ec2types.Filter{{Name: aws.String("vpc-id"), Values: []string{network.VPCID}}}
	tables, err := client.DescribeRouteTables(ctx, &ec2.DescribeRouteTablesInput{Filters: inVPC})
	if err != nil {
		t.Fatal(err)
	}
	var table ec2types.RouteTable
	for _, rt := range tables.RouteTables {
		if slices.ContainsFunc(rt.Routes, func(r ec2types.Route) bool {
			return aws.ToString(r.DestinationCidrBlock) == "0.0.0.0/0" && len(aws.ToString(r.GatewayId)) > 4 && aws.ToString(r.GatewayId)[:4] == "igw-"
		}) {
			table = rt
		}
	}
	if table.RouteTableId == nil {
		t.Fatalf("no route table routes 0.0.0.0/0 to a gateway: %+v", tables.RouteTables)
	}
	subnets, err := client.DescribeSubnets(ctx, &ec2.DescribeSubnetsInput{Filters: inVPC})
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range subnets.Subnets {
		if !aws.ToBool(s.MapPublicIpOnLaunch) {
			t.Errorf("subnet %s does not map public addresses", aws.ToString(s.SubnetId))
		}
		if !slices.ContainsFunc(table.Associations, func(a ec2types.RouteTableAssociation) bool {
			return aws.ToString(a.SubnetId) == aws.ToString(s.SubnetId)
		}) {
			t.Errorf("subnet %s is not on the route table", aws.ToString(s.SubnetId))
		}
	}
	groups, err := client.DescribeSecurityGroups(ctx, &ec2.DescribeSecurityGroupsInput{GroupIds: []string{network.SecurityGroupID}})
	if err != nil {
		t.Fatal(err)
	}
	group := groups.SecurityGroups[0]
	if aws.ToString(group.GroupName) != fleet+"-node" {
		t.Errorf("security group named %s", aws.ToString(group.GroupName))
	}
	if !slices.ContainsFunc(group.IpPermissions, func(p ec2types.IpPermission) bool {
		return aws.ToInt32(p.FromPort) == 12122 && aws.ToInt32(p.ToPort) == 12122 && aws.ToString(p.IpProtocol) == "tcp" &&
			len(p.UserIdGroupPairs) == 1 && aws.ToString(p.UserIdGroupPairs[0].GroupId) == network.SecurityGroupID
	}) {
		t.Errorf("no peer ingress rule: %+v", group.IpPermissions)
	}
	if !slices.ContainsFunc(group.IpPermissionsEgress, func(p ec2types.IpPermission) bool {
		return aws.ToString(p.IpProtocol) == "-1" && slices.ContainsFunc(p.IpRanges, func(r ec2types.IpRange) bool { return aws.ToString(r.CidrIp) == "0.0.0.0/0" })
	}) {
		t.Errorf("no egress to everywhere: %+v", group.IpPermissionsEgress)
	}
	endpoints, err := client.DescribeVpcEndpoints(ctx, &ec2.DescribeVpcEndpointsInput{Filters: inVPC})
	if err != nil {
		t.Fatal(err)
	}
	if len(endpoints.VpcEndpoints) != 1 || aws.ToString(endpoints.VpcEndpoints[0].ServiceName) != "com.amazonaws."+region+".s3" ||
		!slices.Contains(endpoints.VpcEndpoints[0].RouteTableIds, aws.ToString(table.RouteTableId)) {
		t.Errorf("endpoints %+v, want the S3 gateway on %s", endpoints.VpcEndpoints, aws.ToString(table.RouteTableId))
	}
}

// fleetResources lists the ids of the live resources tagged for fleet by
// kind. It reads each kind's describe call: DescribeTags keeps listing the
// tags of a deleted endpoint or rule for a while.
func fleetResources(t *testing.T, client *ec2.Client, fleet string) map[string][]string {
	t.Helper()
	ctx := t.Context()
	tagged := []ec2types.Filter{{Name: aws.String("tag:lazycloud:fleet"), Values: []string{fleet}}}
	out := map[string][]string{}
	add := func(kind string, id *string) { out[kind] = append(out[kind], aws.ToString(id)) }
	vpcs, err := client.DescribeVpcs(ctx, &ec2.DescribeVpcsInput{Filters: tagged})
	if err != nil {
		t.Fatal(err)
	}
	for _, v := range vpcs.Vpcs {
		add("vpc", v.VpcId)
	}
	subnets, err := client.DescribeSubnets(ctx, &ec2.DescribeSubnetsInput{Filters: tagged})
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range subnets.Subnets {
		add("subnet", s.SubnetId)
	}
	gateways, err := client.DescribeInternetGateways(ctx, &ec2.DescribeInternetGatewaysInput{Filters: tagged})
	if err != nil {
		t.Fatal(err)
	}
	for _, g := range gateways.InternetGateways {
		add("internet-gateway", g.InternetGatewayId)
	}
	tables, err := client.DescribeRouteTables(ctx, &ec2.DescribeRouteTablesInput{Filters: tagged})
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range tables.RouteTables {
		add("route-table", r.RouteTableId)
	}
	groups, err := client.DescribeSecurityGroups(ctx, &ec2.DescribeSecurityGroupsInput{Filters: tagged})
	if err != nil {
		t.Fatal(err)
	}
	for _, g := range groups.SecurityGroups {
		add("security-group", g.GroupId)
	}
	rules, err := client.DescribeSecurityGroupRules(ctx, &ec2.DescribeSecurityGroupRulesInput{Filters: tagged})
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range rules.SecurityGroupRules {
		add("security-group-rule", r.SecurityGroupRuleId)
	}
	endpoints, err := client.DescribeVpcEndpoints(ctx, &ec2.DescribeVpcEndpointsInput{Filters: tagged})
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range endpoints.VpcEndpoints {
		if e.State != ec2types.StateDeleted && e.State != ec2types.StateDeleting {
			add("vpc-endpoint", e.VpcEndpointId)
		}
	}
	for kind := range out {
		slices.Sort(out[kind])
	}
	return out
}
