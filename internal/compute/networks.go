package compute

import (
	"cmp"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"net/netip"
	"slices"
	"strings"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/smithy-go"
)

// The fleet network of one account in one region: a VPC with a public
// subnet per availability zone routed to an internet gateway, a node
// security group that admits the volume peer port from itself, and an S3
// gateway endpoint. Every account gets the same shape; a VPC that already
// carries the fleet tag is adopted with its own CIDR block.
const (
	// networkCIDR is the block of a VPC the fleet creates.
	networkCIDR = "10.86.0.0/16"
	// peerPort is the volume peer port hosts of a region read cached
	// volume frames from each other on.
	peerPort = 12122
	// tagStackID marks a VPC a CloudFormation stack owns; the stack would
	// delete it, so the fleet never adopts one.
	tagStackID = "aws:cloudformation:stack-id"
)

// errNoSubnetRange means the VPC's block has no free subnet range left for
// another zone.
var errNoSubnetRange = errors.New("no free subnet range in the VPC")

// ensureNetwork finds or makes the account's fleet network in region and returns it.
func (c *Compute) ensureNetwork(ctx context.Context, scope awsScope, region string) (Network, error) {
	client := c.aws().ec2(scope, region)
	vpcs, err := c.fleetVPCs(ctx, client)
	if err != nil {
		return Network{}, err
	}
	var vpc ec2types.Vpc
	if len(vpcs) > 0 {
		vpc = vpcs[0]
	} else {
		out, err := client.CreateVpc(ctx, &ec2.CreateVpcInput{
			CidrBlock:         aws.String(networkCIDR),
			TagSpecifications: c.networkTags(ec2types.ResourceTypeVpc, c.fleet.Name+"-fleet"),
		})
		if err != nil {
			return Network{}, refusedOr(err, "create the fleet VPC in %s", region)
		}
		vpc = *out.Vpc
	}
	vpcID := aws.ToString(vpc.VpcId)
	for _, attr := range []*ec2.ModifyVpcAttributeInput{
		{VpcId: vpc.VpcId, EnableDnsSupport: &ec2types.AttributeBooleanValue{Value: aws.Bool(true)}},
		{VpcId: vpc.VpcId, EnableDnsHostnames: &ec2types.AttributeBooleanValue{Value: aws.Bool(true)}},
	} {
		if _, err := client.ModifyVpcAttribute(ctx, attr); err != nil {
			return Network{}, refusedOr(err, "turn on DNS in %s", vpcID)
		}
	}
	gateway, err := c.ensureGateway(ctx, client, vpcID)
	if err != nil {
		return Network{}, err
	}
	table, err := c.ensureRouteTable(ctx, client, vpcID, gateway)
	if err != nil {
		return Network{}, err
	}
	subnets, err := c.ensureSubnets(ctx, client, vpc, table)
	if err != nil {
		return Network{}, err
	}
	group, err := c.ensureSecurityGroup(ctx, client, vpcID)
	if err != nil {
		return Network{}, err
	}
	if err := c.ensureEndpoint(ctx, client, region, vpcID, aws.ToString(table.RouteTableId)); err != nil {
		return Network{}, err
	}
	return Network{VPCID: vpcID, SecurityGroupID: group, Subnets: subnets}, nil
}

// fleetVPCs lists the VPCs tagged for the fleet that no CloudFormation
// stack owns, by id.
func (c *Compute) fleetVPCs(ctx context.Context, client *ec2.Client) ([]ec2types.Vpc, error) {
	out, err := client.DescribeVpcs(ctx, &ec2.DescribeVpcsInput{Filters: []ec2types.Filter{c.fleetFilter()}})
	if err != nil {
		return nil, refusedOr(err, "describe fleet VPCs")
	}
	vpcs := slices.DeleteFunc(out.Vpcs, func(v ec2types.Vpc) bool {
		return slices.ContainsFunc(v.Tags, func(t ec2types.Tag) bool { return aws.ToString(t.Key) == tagStackID })
	})
	slices.SortFunc(vpcs, func(a, b ec2types.Vpc) int { return cmp.Compare(aws.ToString(a.VpcId), aws.ToString(b.VpcId)) })
	return vpcs, nil
}

// ensureGateway returns the internet gateway attached to vpc, attaching a
// detached fleet gateway or a new one.
func (c *Compute) ensureGateway(ctx context.Context, client *ec2.Client, vpc string) (string, error) {
	attached, err := client.DescribeInternetGateways(ctx, &ec2.DescribeInternetGatewaysInput{
		Filters: []ec2types.Filter{{Name: aws.String("attachment.vpc-id"), Values: []string{vpc}}},
	})
	if err != nil {
		return "", refusedOr(err, "describe the internet gateway of %s", vpc)
	}
	if len(attached.InternetGateways) > 0 {
		return aws.ToString(attached.InternetGateways[0].InternetGatewayId), nil
	}
	tagged, err := client.DescribeInternetGateways(ctx, &ec2.DescribeInternetGatewaysInput{Filters: []ec2types.Filter{c.fleetFilter()}})
	if err != nil {
		return "", refusedOr(err, "describe fleet internet gateways")
	}
	var id string
	for _, g := range tagged.InternetGateways {
		if len(g.Attachments) == 0 {
			id = aws.ToString(g.InternetGatewayId)
			break
		}
	}
	if id == "" {
		out, err := client.CreateInternetGateway(ctx, &ec2.CreateInternetGatewayInput{
			TagSpecifications: c.networkTags(ec2types.ResourceTypeInternetGateway, c.fleet.Name+"-fleet"),
		})
		if err != nil {
			return "", refusedOr(err, "create an internet gateway")
		}
		id = aws.ToString(out.InternetGateway.InternetGatewayId)
	}
	if _, err := client.AttachInternetGateway(ctx, &ec2.AttachInternetGatewayInput{InternetGatewayId: aws.String(id), VpcId: aws.String(vpc)}); err != nil {
		return "", refusedOr(err, "attach %s to %s", id, vpc)
	}
	return id, nil
}

// ensureRouteTable returns the route table of vpc that routes 0.0.0.0/0 to
// gateway, adding the route to a fleet table without it or to a new table.
func (c *Compute) ensureRouteTable(ctx context.Context, client *ec2.Client, vpc, gateway string) (ec2types.RouteTable, error) {
	out, err := client.DescribeRouteTables(ctx, &ec2.DescribeRouteTablesInput{
		Filters: []ec2types.Filter{{Name: aws.String("vpc-id"), Values: []string{vpc}}},
	})
	if err != nil {
		return ec2types.RouteTable{}, refusedOr(err, "describe the route tables of %s", vpc)
	}
	var unrouted *ec2types.RouteTable
	for i, t := range out.RouteTables {
		if slices.ContainsFunc(t.Routes, func(r ec2types.Route) bool {
			return aws.ToString(r.DestinationCidrBlock) == "0.0.0.0/0" && aws.ToString(r.GatewayId) == gateway
		}) {
			return t, nil
		}
		if unrouted == nil && hasTag(t.Tags, tagFleet, c.fleet.Name) {
			unrouted = &out.RouteTables[i]
		}
	}
	if unrouted == nil {
		created, err := client.CreateRouteTable(ctx, &ec2.CreateRouteTableInput{
			VpcId: aws.String(vpc), TagSpecifications: c.networkTags(ec2types.ResourceTypeRouteTable, c.fleet.Name+"-fleet"),
		})
		if err != nil {
			return ec2types.RouteTable{}, refusedOr(err, "create a route table in %s", vpc)
		}
		unrouted = created.RouteTable
	}
	if _, err := client.CreateRoute(ctx, &ec2.CreateRouteInput{
		RouteTableId: unrouted.RouteTableId, DestinationCidrBlock: aws.String("0.0.0.0/0"), GatewayId: aws.String(gateway),
	}); err != nil {
		return ec2types.RouteTable{}, refusedOr(err, "route %s to %s", aws.ToString(unrouted.RouteTableId), gateway)
	}
	return *unrouted, nil
}

// ensureSubnets returns one public subnet of vpc in every available zone,
// each associated with table, creating the missing ones in the first free
// /24-sized ranges of the VPC's block as cidrsubnet(cidr, 8, n) for n >= 1.
func (c *Compute) ensureSubnets(ctx context.Context, client *ec2.Client, vpc ec2types.Vpc, table ec2types.RouteTable) ([]Subnet, error) {
	vpcID := aws.ToString(vpc.VpcId)
	zones, err := client.DescribeAvailabilityZones(ctx, &ec2.DescribeAvailabilityZonesInput{Filters: []ec2types.Filter{
		{Name: aws.String("zone-type"), Values: []string{"availability-zone"}},
		{Name: aws.String("state"), Values: []string{"available"}},
	}})
	if err != nil {
		return nil, refusedOr(err, "describe availability zones")
	}
	existing, err := client.DescribeSubnets(ctx, &ec2.DescribeSubnetsInput{
		Filters: []ec2types.Filter{{Name: aws.String("vpc-id"), Values: []string{vpcID}}},
	})
	if err != nil {
		return nil, refusedOr(err, "describe the subnets of %s", vpcID)
	}
	block, err := netip.ParsePrefix(aws.ToString(vpc.CidrBlock))
	if err != nil {
		return nil, fmt.Errorf("VPC %s block: %w", vpcID, err)
	}
	var used []netip.Prefix
	byZone := map[string]ec2types.Subnet{}
	for _, s := range existing.Subnets {
		if p, err := netip.ParsePrefix(aws.ToString(s.CidrBlock)); err == nil {
			used = append(used, p)
		}
		if _, ok := byZone[aws.ToString(s.AvailabilityZone)]; !ok {
			byZone[aws.ToString(s.AvailabilityZone)] = s
		}
	}
	associated := map[string]bool{}
	for _, a := range table.Associations {
		associated[aws.ToString(a.SubnetId)] = true
	}
	subnets := make([]Subnet, 0, len(zones.AvailabilityZones))
	for _, z := range zones.AvailabilityZones {
		zone := aws.ToString(z.ZoneName)
		s, ok := byZone[zone]
		if !ok {
			cidr, err := freeRange(block, used)
			if err != nil {
				return nil, fmt.Errorf("subnet for %s in %s: %w", zone, vpcID, err)
			}
			out, err := client.CreateSubnet(ctx, &ec2.CreateSubnetInput{
				VpcId: vpc.VpcId, AvailabilityZone: aws.String(zone), CidrBlock: aws.String(cidr.String()),
				TagSpecifications: c.networkTags(ec2types.ResourceTypeSubnet, c.fleet.Name+"-"+zone),
			})
			if err != nil {
				return nil, refusedOr(err, "create a subnet in %s", zone)
			}
			used = append(used, cidr)
			s = *out.Subnet
		}
		if !aws.ToBool(s.MapPublicIpOnLaunch) {
			if _, err := client.ModifySubnetAttribute(ctx, &ec2.ModifySubnetAttributeInput{
				SubnetId: s.SubnetId, MapPublicIpOnLaunch: &ec2types.AttributeBooleanValue{Value: aws.Bool(true)},
			}); err != nil {
				return nil, refusedOr(err, "map public addresses in %s", aws.ToString(s.SubnetId))
			}
		}
		if !associated[aws.ToString(s.SubnetId)] {
			if _, err := client.AssociateRouteTable(ctx, &ec2.AssociateRouteTableInput{RouteTableId: table.RouteTableId, SubnetId: s.SubnetId}); err != nil {
				return nil, refusedOr(err, "associate %s with %s", aws.ToString(s.SubnetId), aws.ToString(table.RouteTableId))
			}
		}
		subnets = append(subnets, Subnet{ID: aws.ToString(s.SubnetId), Zone: zone, ZoneID: aws.ToString(z.ZoneId)})
	}
	slices.SortFunc(subnets, func(a, b Subnet) int { return cmp.Compare(a.Zone, b.Zone) })
	return subnets, nil
}

// freeRange is the first cidrsubnet(block, 8, n), n >= 1, that overlaps no
// used range.
func freeRange(block netip.Prefix, used []netip.Prefix) (netip.Prefix, error) {
	bits := block.Bits() + 8
	if !block.Addr().Is4() || bits > 32 {
		return netip.Prefix{}, fmt.Errorf("%w: %s", errNoSubnetRange, block)
	}
	addr := block.Masked().Addr().As4()
	start := binary.BigEndian.Uint32(addr[:])
	for n := uint32(1); n < 256; n++ {
		binary.BigEndian.PutUint32(addr[:], start+n<<(32-bits))
		p := netip.PrefixFrom(netip.AddrFrom4(addr), bits)
		if !slices.ContainsFunc(used, p.Overlaps) {
			return p, nil
		}
	}
	return netip.Prefix{}, fmt.Errorf("%w: %s", errNoSubnetRange, block)
}

// ensureSecurityGroup returns the node security group of vpc, the one it
// tags for the fleet, admitting the volume peer port from itself.
func (c *Compute) ensureSecurityGroup(ctx context.Context, client *ec2.Client, vpc string) (string, error) {
	name := c.fleet.Name + "-node"
	out, err := client.DescribeSecurityGroups(ctx, &ec2.DescribeSecurityGroupsInput{Filters: []ec2types.Filter{
		{Name: aws.String("vpc-id"), Values: []string{vpc}}, c.fleetFilter(),
	}})
	if err != nil {
		return "", refusedOr(err, "describe the node security group of %s", vpc)
	}
	var group ec2types.SecurityGroup
	if len(out.SecurityGroups) > 0 {
		group = out.SecurityGroups[0]
	} else {
		created, err := client.CreateSecurityGroup(ctx, &ec2.CreateSecurityGroupInput{
			GroupName: aws.String(name), Description: aws.String("LazyCloud fleet hosts"), VpcId: aws.String(vpc),
			TagSpecifications: c.networkTags(ec2types.ResourceTypeSecurityGroup, name),
		})
		if err != nil {
			return "", refusedOr(err, "create the node security group in %s", vpc)
		}
		group.GroupId = created.GroupId
	}
	id := aws.ToString(group.GroupId)
	if slices.ContainsFunc(group.IpPermissions, func(p ec2types.IpPermission) bool {
		return aws.ToString(p.IpProtocol) == "tcp" && aws.ToInt32(p.FromPort) == peerPort && aws.ToInt32(p.ToPort) == peerPort &&
			slices.ContainsFunc(p.UserIdGroupPairs, func(g ec2types.UserIdGroupPair) bool { return aws.ToString(g.GroupId) == id })
	}) {
		return id, nil
	}
	if _, err := client.AuthorizeSecurityGroupIngress(ctx, &ec2.AuthorizeSecurityGroupIngressInput{
		GroupId: aws.String(id),
		IpPermissions: []ec2types.IpPermission{{
			IpProtocol: aws.String("tcp"), FromPort: aws.Int32(peerPort), ToPort: aws.Int32(peerPort),
			UserIdGroupPairs: []ec2types.UserIdGroupPair{{GroupId: aws.String(id)}},
		}},
		TagSpecifications: c.networkTags(ec2types.ResourceTypeSecurityGroupRule, name),
	}); err != nil {
		return "", refusedOr(err, "admit the volume peer port in %s", id)
	}
	return id, nil
}

// ensureEndpoint gives vpc an S3 gateway endpoint on table.
func (c *Compute) ensureEndpoint(ctx context.Context, client *ec2.Client, region, vpc, table string) error {
	service := "com.amazonaws." + region + ".s3"
	endpoints, err := c.liveEndpoints(ctx, client, vpc, service)
	if err != nil {
		return err
	}
	if len(endpoints) > 0 {
		return nil
	}
	if _, err := client.CreateVpcEndpoint(ctx, &ec2.CreateVpcEndpointInput{
		VpcId: aws.String(vpc), ServiceName: aws.String(service), VpcEndpointType: ec2types.VpcEndpointTypeGateway,
		RouteTableIds:     []string{table},
		TagSpecifications: c.networkTags(ec2types.ResourceTypeVpcEndpoint, c.fleet.Name+"-s3"),
	}); err != nil {
		return refusedOr(err, "create the S3 endpoint of %s", vpc)
	}
	return nil
}

// liveEndpoints lists the gateway endpoints of vpc not being deleted, for
// service or, with service empty, for any service.
func (c *Compute) liveEndpoints(ctx context.Context, client *ec2.Client, vpc, service string) ([]ec2types.VpcEndpoint, error) {
	filters := []ec2types.Filter{
		{Name: aws.String("vpc-id"), Values: []string{vpc}},
		{Name: aws.String("vpc-endpoint-type"), Values: []string{"Gateway"}},
	}
	if service != "" {
		filters = append(filters, ec2types.Filter{Name: aws.String("service-name"), Values: []string{service}})
	}
	out, err := client.DescribeVpcEndpoints(ctx, &ec2.DescribeVpcEndpointsInput{Filters: filters})
	if err != nil {
		return nil, refusedOr(err, "describe the endpoints of %s", vpc)
	}
	return slices.DeleteFunc(out.VpcEndpoints, func(e ec2types.VpcEndpoint) bool {
		s := strings.ToLower(string(e.State))
		return s == "deleting" || s == "deleted"
	}), nil
}

// deleteNetwork deletes the account's fleet network in region; gone reports nothing of it is left.
// A dependency AWS still holds (an ENI of a just-terminated instance) returns gone=false, nil error, so the caller retries later.
func (c *Compute) deleteNetwork(ctx context.Context, scope awsScope, region string) (gone bool, err error) {
	client := c.aws().ec2(scope, region)
	vpcs, err := c.fleetVPCs(ctx, client)
	if err != nil {
		return false, err
	}
	for _, v := range vpcs {
		if err := c.deleteVPC(ctx, client, aws.ToString(v.VpcId)); err != nil {
			if awsCode(err) == "DependencyViolation" {
				return false, nil
			}
			return false, err
		}
	}
	return true, nil
}

// deleteVPC deletes vpc after everything in it that blocks the deletion.
func (c *Compute) deleteVPC(ctx context.Context, client *ec2.Client, vpc string) error {
	inVPC := []ec2types.Filter{{Name: aws.String("vpc-id"), Values: []string{vpc}}}
	endpoints, err := c.liveEndpoints(ctx, client, vpc, "")
	if err != nil {
		return err
	}
	if len(endpoints) > 0 {
		ids := make([]string, 0, len(endpoints))
		for _, e := range endpoints {
			ids = append(ids, aws.ToString(e.VpcEndpointId))
		}
		out, err := client.DeleteVpcEndpoints(ctx, &ec2.DeleteVpcEndpointsInput{VpcEndpointIds: ids})
		if err != nil {
			return refusedOr(err, "delete the endpoints of %s", vpc)
		}
		for _, u := range out.Unsuccessful {
			if u.Error != nil && !alreadyGone(aws.ToString(u.Error.Code)) {
				return fmt.Errorf("delete endpoint %s: %w", aws.ToString(u.ResourceId),
					&smithy.GenericAPIError{Code: aws.ToString(u.Error.Code), Message: aws.ToString(u.Error.Message)})
			}
		}
	}
	subnets, err := client.DescribeSubnets(ctx, &ec2.DescribeSubnetsInput{Filters: inVPC})
	if err != nil {
		return refusedOr(err, "describe the subnets of %s", vpc)
	}
	for _, s := range subnets.Subnets {
		if _, err := client.DeleteSubnet(ctx, &ec2.DeleteSubnetInput{SubnetId: s.SubnetId}); err != nil && !alreadyGone(awsCode(err)) {
			return refusedOr(err, "delete subnet %s", aws.ToString(s.SubnetId))
		}
	}
	tables, err := client.DescribeRouteTables(ctx, &ec2.DescribeRouteTablesInput{Filters: inVPC})
	if err != nil {
		return refusedOr(err, "describe the route tables of %s", vpc)
	}
	for _, t := range tables.RouteTables {
		if slices.ContainsFunc(t.Associations, func(a ec2types.RouteTableAssociation) bool { return aws.ToBool(a.Main) }) {
			continue
		}
		if _, err := client.DeleteRouteTable(ctx, &ec2.DeleteRouteTableInput{RouteTableId: t.RouteTableId}); err != nil && !alreadyGone(awsCode(err)) {
			return refusedOr(err, "delete route table %s", aws.ToString(t.RouteTableId))
		}
	}
	groups, err := client.DescribeSecurityGroups(ctx, &ec2.DescribeSecurityGroupsInput{Filters: inVPC})
	if err != nil {
		return refusedOr(err, "describe the security groups of %s", vpc)
	}
	for _, g := range groups.SecurityGroups {
		if aws.ToString(g.GroupName) == "default" {
			continue
		}
		if _, err := client.DeleteSecurityGroup(ctx, &ec2.DeleteSecurityGroupInput{GroupId: g.GroupId}); err != nil && !alreadyGone(awsCode(err)) {
			return refusedOr(err, "delete security group %s", aws.ToString(g.GroupId))
		}
	}
	gateways, err := client.DescribeInternetGateways(ctx, &ec2.DescribeInternetGatewaysInput{
		Filters: []ec2types.Filter{{Name: aws.String("attachment.vpc-id"), Values: []string{vpc}}},
	})
	if err != nil {
		return refusedOr(err, "describe the internet gateway of %s", vpc)
	}
	for _, g := range gateways.InternetGateways {
		if _, err := client.DetachInternetGateway(ctx, &ec2.DetachInternetGatewayInput{InternetGatewayId: g.InternetGatewayId, VpcId: aws.String(vpc)}); err != nil && !alreadyGone(awsCode(err)) {
			return refusedOr(err, "detach %s", aws.ToString(g.InternetGatewayId))
		}
		if _, err := client.DeleteInternetGateway(ctx, &ec2.DeleteInternetGatewayInput{InternetGatewayId: g.InternetGatewayId}); err != nil && !alreadyGone(awsCode(err)) {
			return refusedOr(err, "delete %s", aws.ToString(g.InternetGatewayId))
		}
	}
	if _, err := client.DeleteVpc(ctx, &ec2.DeleteVpcInput{VpcId: aws.String(vpc)}); err != nil && !alreadyGone(awsCode(err)) {
		return refusedOr(err, "delete %s", vpc)
	}
	return nil
}

// alreadyGone reports whether an EC2 error code says the resource is already
// deleted or detached.
func alreadyGone(code string) bool {
	return strings.HasSuffix(code, ".NotFound") || code == "Gateway.NotAttached"
}

func (c *Compute) fleetFilter() ec2types.Filter {
	return ec2types.Filter{Name: aws.String("tag:" + tagFleet), Values: []string{c.fleet.Name}}
}

// networkTags tags a network resource at creation with the fleet and name.
func (c *Compute) networkTags(kind ec2types.ResourceType, name string) []ec2types.TagSpecification {
	return []ec2types.TagSpecification{{ResourceType: kind, Tags: []ec2types.Tag{
		{Key: aws.String(tagFleet), Value: aws.String(c.fleet.Name)},
		{Key: aws.String("Name"), Value: aws.String(name)},
	}}}
}

func hasTag(tags []ec2types.Tag, key, value string) bool {
	return slices.ContainsFunc(tags, func(t ec2types.Tag) bool { return aws.ToString(t.Key) == key && aws.ToString(t.Value) == value })
}
