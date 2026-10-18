package compute_test

import (
	"reflect"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

const networkFleet = "lazycloud-test"

func newNetworkOwner(t *testing.T) (*compute.Compute, *fakeVPCs) {
	t.Helper()
	e := newAWS(t)
	f := newFakeVPCs(e)
	return compute.NewCompute(nil, nil, compute.Config{Fleet: e.fleet(compute.Fleet{Name: networkFleet})}), f
}

// checkNetwork checks the fake holds exactly one fleet network, the one n
// names: a VPC with DNS on, one attached gateway, one route table routing
// 0.0.0.0/0 to it, one public subnet per zone on that table, the node group
// admitting the peer port from itself and the S3 endpoint on the table.
func checkNetwork(t *testing.T, f *fakeVPCs, n compute.Network) {
	t.Helper()
	f.with(func() {
		vpc := f.vpcs[n.VPCID]
		if vpc == nil || !vpc.DNSSupport || !vpc.DNSHostnames || vpc.Tags["lazycloud:fleet"] != networkFleet {
			t.Fatalf("VPC %s: %+v", n.VPCID, vpc)
		}
		var gateways []string
		for _, g := range f.gateways {
			if g.VPC == n.VPCID || g.VPC == "" {
				gateways = append(gateways, g.ID)
			}
		}
		var tables []*fakeTable
		for _, rt := range f.tables {
			if rt.VPC == n.VPCID && !rt.Main {
				tables = append(tables, rt)
			}
		}
		if len(gateways) != 1 || len(tables) != 1 || tables[0].Routes["0.0.0.0/0"] != gateways[0] {
			t.Fatalf("gateways %v, route tables %+v", gateways, tables)
		}
		var want []compute.Subnet
		for _, s := range f.subnets {
			if s.VPC == n.VPCID {
				if !s.Public {
					t.Errorf("subnet %s does not map public addresses", s.ID)
				}
				want = append(want, compute.Subnet{ID: s.ID, Zone: s.Zone, ZoneID: s.ZoneID})
			}
		}
		if len(want) != len(fakeZones()) || len(tables[0].Subnets) != len(want) {
			t.Errorf("subnets %+v, on the route table %v", want, tables[0].Subnets)
		}
		for i, z := range fakeZones() {
			if i >= len(n.Subnets) || n.Subnets[i].Zone != z[0] || n.Subnets[i].ZoneID != z[1] {
				t.Fatalf("network subnets %+v, want one per zone in zone order", n.Subnets)
			}
		}
		var groups []*fakeGroup
		for _, g := range f.groups {
			if g.VPC == n.VPCID && g.Name != "default" {
				groups = append(groups, g)
			}
		}
		if len(groups) != 1 || groups[0].ID != n.SecurityGroupID ||
			!reflect.DeepEqual(groups[0].Peers, []string{n.SecurityGroupID}) {
			t.Errorf("node groups %+v", groups)
		}
		var endpoints []*fakeEndpoint
		for _, e := range f.endpoints {
			if e.VPC == n.VPCID {
				endpoints = append(endpoints, e)
			}
		}
		if len(endpoints) != 1 || endpoints[0].Service != "com.amazonaws.us-east-2.s3" || !reflect.DeepEqual(endpoints[0].Tables, []string{tables[0].ID}) {
			t.Errorf("endpoints %+v", endpoints)
		}
	})
}

// TestNetworkMadeInAFreshAccountAndDeleted makes the network beside a VPC an
// old connection stack tagged for the fleet, which the network neither
// adopts nor deletes, and deletes it once AWS releases a held subnet.
func TestNetworkMadeInAFreshAccountAndDeleted(t *testing.T) {
	t.Parallel()
	c, f := newNetworkOwner(t)
	f.with(func() {
		f.vpcs["vpc-stack"] = &fakeVPC{ID: "vpc-stack", CIDR: "10.0.0.0/16", Tags: map[string]string{
			"lazycloud:fleet": networkFleet, "aws:cloudformation:stack-id": "arn:aws:cloudformation:us-east-2:534742592531:stack/lazycloud-connection/1",
		}}
	})

	n, err := c.EnsurePlatformNetwork(t.Context(), "us-east-2")
	if err != nil {
		t.Fatal(err)
	}
	checkNetwork(t, f, n)
	f.with(func() {
		if cidr := f.vpcs[n.VPCID].CIDR; cidr != "10.86.0.0/16" {
			t.Errorf("new VPC block %s", cidr)
		}
		tagged := func(kind, id string, tags map[string]string) {
			if tags["lazycloud:fleet"] != networkFleet {
				t.Errorf("%s %s tags %v", kind, id, tags)
			}
		}
		for _, s := range f.subnets {
			tagged("subnet", s.ID, s.Tags)
		}
		for _, g := range f.gateways {
			tagged("gateway", g.ID, g.Tags)
		}
		for _, rt := range f.tables {
			if !rt.Main {
				tagged("route table", rt.ID, rt.Tags)
			}
		}
		tagged("group", n.SecurityGroupID, f.groups[n.SecurityGroupID].Tags)
		for _, e := range f.endpoints {
			tagged("endpoint", e.ID, e.Tags)
		}
	})
	again, err := c.EnsurePlatformNetwork(t.Context(), "us-east-2")
	if err != nil || !reflect.DeepEqual(again, n) {
		t.Fatalf("ensure again: %+v, %v; want %+v", again, err, n)
	}
	checkNetwork(t, f, n)

	f.with(func() { f.held[n.Subnets[1].ID] = true })
	if gone, err := c.DeletePlatformNetwork(t.Context(), "us-east-2"); gone || err != nil {
		t.Fatalf("delete with a held subnet: gone %t, %v; want not gone and no error", gone, err)
	}
	f.with(func() { delete(f.held, n.Subnets[1].ID) })
	if gone, err := c.DeletePlatformNetwork(t.Context(), "us-east-2"); !gone || err != nil {
		t.Fatalf("delete: gone %t, %v", gone, err)
	}
	f.with(func() {
		if len(f.vpcs) != 1 || f.vpcs["vpc-stack"] == nil || len(f.subnets)+len(f.gateways)+len(f.tables)+len(f.groups)+len(f.endpoints) != 0 {
			t.Errorf("left VPCs %v, subnets %v, gateways %v, tables %v, groups %v, endpoints %v", f.vpcs, f.subnets, f.gateways, f.tables, f.groups, f.endpoints)
		}
	})
}

// TestNetworkFillsInWhatAnExistingNetworkLacks completes an existing fleet
// network, whose gateway and route table carry only a Name tag, without
// replacing any of it: a zone AWS opened since gets the first free range of
// the VPC's own block, and an unassociated subnet and a missing peer rule
// are added.
func TestNetworkFillsInWhatAnExistingNetworkLacks(t *testing.T) {
	t.Parallel()
	c, f := newNetworkOwner(t)
	fleetTags := func(name string) map[string]string {
		return map[string]string{"lazycloud:fleet": networkFleet, "Name": name}
	}
	f.with(func() {
		f.vpcs["vpc-tf"] = &fakeVPC{ID: "vpc-tf", CIDR: "10.50.0.0/16", DNSSupport: true, DNSHostnames: true, Tags: fleetTags("lazycloud")}
		f.tables["rtb-main"] = &fakeTable{ID: "rtb-main", VPC: "vpc-tf", Main: true, Routes: map[string]string{"10.50.0.0/16": "local"}}
		f.groups["sg-default"] = &fakeGroup{ID: "sg-default", VPC: "vpc-tf", Name: "default"}
		f.gateways["igw-tf"] = &fakeGateway{ID: "igw-tf", VPC: "vpc-tf", Tags: map[string]string{"Name": "lazycloud"}}
		f.tables["rtb-tf"] = &fakeTable{
			ID: "rtb-tf", VPC: "vpc-tf", Routes: map[string]string{"10.50.0.0/16": "local", "0.0.0.0/0": "igw-tf"},
			Subnets: []string{"subnet-a"}, Tags: map[string]string{"Name": "lazycloud"},
		}
		f.subnets["subnet-a"] = &fakeSubnet{ID: "subnet-a", VPC: "vpc-tf", Zone: "us-east-2a", ZoneID: "use2-az1", CIDR: "10.50.1.0/24", Public: true, Tags: fleetTags("lazycloud-us-east-2a")}
		f.subnets["subnet-b"] = &fakeSubnet{ID: "subnet-b", VPC: "vpc-tf", Zone: "us-east-2b", ZoneID: "use2-az2", CIDR: "10.50.3.0/24", Public: true, Tags: fleetTags("lazycloud-us-east-2b")}
		f.groups["sg-tf"] = &fakeGroup{ID: "sg-tf", VPC: "vpc-tf", Name: networkFleet + "-fleet-node", Tags: fleetTags(networkFleet + "-fleet-node")}
		f.endpoints["vpce-tf"] = &fakeEndpoint{ID: "vpce-tf", VPC: "vpc-tf", Service: "com.amazonaws.us-east-2.s3", Tables: []string{"rtb-tf"}, Tags: map[string]string{"Name": "lazycloud-s3"}}
	})

	n, err := c.EnsurePlatformNetwork(t.Context(), "us-east-2")
	if err != nil {
		t.Fatal(err)
	}
	checkNetwork(t, f, n)
	if n.VPCID != "vpc-tf" || n.SecurityGroupID != "sg-tf" || n.Subnets[0].ID != "subnet-a" || n.Subnets[1].ID != "subnet-b" {
		t.Fatalf("network %+v, want the Terraform one", n)
	}
	f.with(func() {
		if f.vpcs["vpc-tf"].CIDR != "10.50.0.0/16" || f.gateways["igw-tf"] == nil || f.tables["rtb-tf"] == nil || f.endpoints["vpce-tf"] == nil {
			t.Fatal("the Terraform network was replaced")
		}
		if s := f.subnets[n.Subnets[2].ID]; s.CIDR != "10.50.2.0/24" {
			t.Errorf("new subnet in %s got %s, want the first free range 10.50.2.0/24", s.Zone, s.CIDR)
		}
	})
}

// TestNetworkCompletesAfterALostAnswer loses the answer to each call that
// creates something, after EC2 applied it; the next ensure finds what the
// lost call made and completes the network without a duplicate.
func TestNetworkCompletesAfterALostAnswer(t *testing.T) {
	t.Parallel()
	for _, action := range []string{
		"CreateVpc", "CreateInternetGateway", "AttachInternetGateway", "CreateRouteTable", "CreateRoute",
		"CreateSubnet", "ModifySubnetAttribute", "AssociateRouteTable", "CreateSecurityGroup",
		"AuthorizeSecurityGroupIngress", "CreateVpcEndpoint",
	} {
		t.Run(action, func(t *testing.T) {
			t.Parallel()
			c, f := newNetworkOwner(t)
			f.with(func() { f.lose = action })
			if _, err := c.EnsurePlatformNetwork(t.Context(), "us-east-2"); err == nil {
				t.Fatal("ensure succeeded with a lost answer")
			}
			n, err := c.EnsurePlatformNetwork(t.Context(), "us-east-2")
			if err != nil {
				t.Fatal(err)
			}
			checkNetwork(t, f, n)
			f.with(func() {
				if len(f.vpcs) != 1 {
					t.Errorf("VPCs %v", f.vpcs)
				}
			})
		})
	}
}
