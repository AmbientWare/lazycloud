package compute_test

import (
	"fmt"
	"maps"
	"net/http"
	"slices"
	"strings"
	"sync"
)

// fakeVPCs is the network side of one EC2 region, us-east-2, on the
// emulator: VPCs, subnets, internet gateways, route tables, security groups
// and gateway endpoints, changed by the calls ensureNetwork and
// deleteNetwork make and read back with the filters they send. Its answers
// and errors follow TestRealEC2Network's wire log, recorded from EC2
// us-east-2 in account 534742592531 on 2026-10-17: CreateVpc makes the main
// route table and the default group with it, CreateSubnet leaves
// mapPublicIpOnLaunch false, each delete of a missing resource answers its
// *.NotFound code, DeleteVpcEndpoints reports a missing endpoint as an
// unsuccessful item instead of an error, and DeleteVpc answers
// DependencyViolation while anything is left in the VPC.
type fakeVPCs struct {
	mu        sync.Mutex
	next      int
	vpcs      map[string]*fakeVPC
	subnets   map[string]*fakeSubnet
	gateways  map[string]*fakeGateway
	tables    map[string]*fakeTable
	groups    map[string]*fakeGroup
	endpoints map[string]*fakeEndpoint
	// held names subnets an ENI AWS has not released yet still uses.
	held map[string]bool
	// lose is an action whose next call EC2 applies but whose answer is
	// lost, as a dropped connection leaves it.
	lose string
}

type fakeVPC struct {
	ID, CIDR                 string
	DNSSupport, DNSHostnames bool
	Tags                     map[string]string
}

type fakeSubnet struct {
	ID, VPC, Zone, ZoneID, CIDR string
	Public                      bool
	Tags                        map[string]string
}

type fakeGateway struct {
	ID string
	// VPC is the attached VPC, or "".
	VPC  string
	Tags map[string]string
}

type fakeTable struct {
	ID, VPC string
	Main    bool
	// Routes maps a destination block to its gateway.
	Routes  map[string]string
	Subnets []string
	Tags    map[string]string
}

type fakeGroup struct {
	ID, VPC, Name string
	// Peers is the groups the volume peer port admits.
	Peers []string
	Tags  map[string]string
}

type fakeEndpoint struct {
	ID, VPC, Service string
	Tables           []string
	Tags             map[string]string
}

// fakeZones are us-east-2's availability zones and zone ids as
// DescribeAvailabilityZones listed them.
func fakeZones() [][2]string {
	return [][2]string{{"us-east-2a", "use2-az1"}, {"us-east-2b", "use2-az2"}, {"us-east-2c", "use2-az3"}}
}

const ec2NS = `xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"`

func newFakeVPCs(e *awsEmulator) *fakeVPCs {
	f := &fakeVPCs{
		vpcs: map[string]*fakeVPC{}, subnets: map[string]*fakeSubnet{}, gateways: map[string]*fakeGateway{},
		tables: map[string]*fakeTable{}, groups: map[string]*fakeGroup{}, endpoints: map[string]*fakeEndpoint{},
		held: map[string]bool{},
	}
	for action, h := range map[string]func(awsCall) awsReply{
		"DescribeVpcs": f.describeVpcs, "CreateVpc": f.createVpc, "ModifyVpcAttribute": f.modifyVpcAttribute, "DeleteVpc": f.deleteVpc,
		"DescribeInternetGateways": f.describeGateways, "CreateInternetGateway": f.createGateway,
		"AttachInternetGateway": f.attachGateway, "DetachInternetGateway": f.detachGateway, "DeleteInternetGateway": f.deleteGateway,
		"DescribeRouteTables": f.describeTables, "CreateRouteTable": f.createTable, "CreateRoute": f.createRoute,
		"AssociateRouteTable": f.associate, "DeleteRouteTable": f.deleteTable,
		"DescribeAvailabilityZones": f.describeZones, "DescribeSubnets": f.describeSubnets, "CreateSubnet": f.createSubnet,
		"ModifySubnetAttribute": f.modifySubnetAttribute, "DeleteSubnet": f.deleteSubnet,
		"DescribeSecurityGroups": f.describeGroups, "CreateSecurityGroup": f.createGroup,
		"AuthorizeSecurityGroupIngress": f.authorize, "DeleteSecurityGroup": f.deleteGroup,
		"DescribeVpcEndpoints": f.describeEndpoints, "CreateVpcEndpoint": f.createEndpoint, "DeleteVpcEndpoints": f.deleteEndpoints,
	} {
		e.on(action, func(call awsCall) awsReply {
			f.mu.Lock()
			defer f.mu.Unlock()
			reply := h(call)
			if f.lose == call.Action {
				f.lose = ""
				return dropped()
			}
			return reply
		})
	}
	return f
}

// with runs fn holding the fake's lock, to seed or read its state.
func (f *fakeVPCs) with(fn func()) {
	f.mu.Lock()
	defer f.mu.Unlock()
	fn()
}

func (f *fakeVPCs) id(prefix string) string {
	f.next++
	return fmt.Sprintf("%s-%017x", prefix, f.next)
}

func reply(action, inner string) awsReply {
	return ok(fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<%[1]sResponse %[2]s><requestId>req-0000</requestId>%[3]s</%[1]sResponse>`, action, ec2NS, inner))
}

func done(action string) awsReply { return reply(action, `<return>true</return>`) }

func tagSet(tags map[string]string) string {
	var b strings.Builder
	b.WriteString(`<tagSet>`)
	writeTags(&b, tags)
	b.WriteString(`</tagSet>`)
	return b.String()
}

// pass reports whether value meets the filter name in want, if want has it.
func pass(want map[string][]string, name, value string) bool {
	v, ok := want[name]
	return !ok || slices.Contains(v, value)
}

func sortedKeys[V any](m map[string]V) []string { return slices.Sorted(maps.Keys(m)) }

func notFound(code, what, id string) awsReply {
	return ec2Error(http.StatusBadRequest, code, fmt.Sprintf("The %s '%s' does not exist", what, id))
}

func dependencyViolation(what, id string) awsReply {
	return ec2Error(http.StatusBadRequest, "DependencyViolation", fmt.Sprintf("The %s '%s' has dependencies and cannot be deleted.", what, id))
}

func (f *fakeVPCs) vpcXML(v *fakeVPC) string {
	return fmt.Sprintf(`<vpcId>%s</vpcId><ownerId>534742592531</ownerId><state>available</state><cidrBlock>%s</cidrBlock>%s<instanceTenancy>default</instanceTenancy><isDefault>false</isDefault>`,
		esc(v.ID), esc(v.CIDR), tagSet(v.Tags))
}

func (f *fakeVPCs) describeVpcs(call awsCall) awsReply {
	want := filters(call.Form)
	var b strings.Builder
	for _, id := range sortedKeys(f.vpcs) {
		if v := f.vpcs[id]; matches(v.Tags, want) {
			b.WriteString(`<item>` + f.vpcXML(v) + `</item>`)
		}
	}
	return reply("DescribeVpcs", `<vpcSet>`+b.String()+`</vpcSet>`)
}

func (f *fakeVPCs) createVpc(call awsCall) awsReply {
	v := &fakeVPC{ID: f.id("vpc"), CIDR: call.Form.Get("CidrBlock"), DNSSupport: true, Tags: specTags(call.Form, "vpc")}
	f.vpcs[v.ID] = v
	main := f.id("rtb")
	f.tables[main] = &fakeTable{ID: main, VPC: v.ID, Main: true, Routes: map[string]string{v.CIDR: "local"}}
	group := f.id("sg")
	f.groups[group] = &fakeGroup{ID: group, VPC: v.ID, Name: "default"}
	return reply("CreateVpc", `<vpc>`+strings.Replace(f.vpcXML(v), "available", "pending", 1)+`</vpc>`)
}

func (f *fakeVPCs) modifyVpcAttribute(call awsCall) awsReply {
	v, ok := f.vpcs[call.Form.Get("VpcId")]
	if !ok {
		return notFound("InvalidVpcID.NotFound", "vpc ID", call.Form.Get("VpcId"))
	}
	if s := call.Form.Get("EnableDnsSupport.Value"); s != "" {
		v.DNSSupport = s == "true"
	}
	if s := call.Form.Get("EnableDnsHostnames.Value"); s != "" {
		v.DNSHostnames = s == "true"
	}
	return done("ModifyVpcAttribute")
}

func (f *fakeVPCs) deleteVpc(call awsCall) awsReply {
	id := call.Form.Get("VpcId")
	if _, ok := f.vpcs[id]; !ok {
		return notFound("InvalidVpcID.NotFound", "vpc ID", id)
	}
	for _, s := range f.subnets {
		if s.VPC == id {
			return dependencyViolation("vpc", id)
		}
	}
	for _, g := range f.gateways {
		if g.VPC == id {
			return dependencyViolation("vpc", id)
		}
	}
	for _, t := range f.tables {
		if t.VPC == id && !t.Main {
			return dependencyViolation("vpc", id)
		}
	}
	for _, g := range f.groups {
		if g.VPC == id && g.Name != "default" {
			return dependencyViolation("vpc", id)
		}
	}
	for _, e := range f.endpoints {
		if e.VPC == id {
			return dependencyViolation("vpc", id)
		}
	}
	maps.DeleteFunc(f.tables, func(_ string, t *fakeTable) bool { return t.VPC == id })
	maps.DeleteFunc(f.groups, func(_ string, g *fakeGroup) bool { return g.VPC == id })
	delete(f.vpcs, id)
	return done("DeleteVpc")
}

func (f *fakeVPCs) gatewayXML(g *fakeGateway) string {
	attachments := `<attachmentSet/>`
	if g.VPC != "" {
		attachments = fmt.Sprintf(`<attachmentSet><item><vpcId>%s</vpcId><state>available</state></item></attachmentSet>`, esc(g.VPC))
	}
	return fmt.Sprintf(`<internetGatewayId>%s</internetGatewayId><ownerId>534742592531</ownerId>%s%s`, esc(g.ID), attachments, tagSet(g.Tags))
}

func (f *fakeVPCs) describeGateways(call awsCall) awsReply {
	want := filters(call.Form)
	var b strings.Builder
	for _, id := range sortedKeys(f.gateways) {
		g := f.gateways[id]
		if v, ok := want["attachment.vpc-id"]; (ok && !slices.Contains(v, g.VPC)) || !matches(g.Tags, want) {
			continue
		}
		b.WriteString(`<item>` + f.gatewayXML(g) + `</item>`)
	}
	return reply("DescribeInternetGateways", `<internetGatewaySet>`+b.String()+`</internetGatewaySet>`)
}

func (f *fakeVPCs) createGateway(call awsCall) awsReply {
	g := &fakeGateway{ID: f.id("igw"), Tags: specTags(call.Form, "internet-gateway")}
	f.gateways[g.ID] = g
	return reply("CreateInternetGateway", `<internetGateway>`+f.gatewayXML(g)+`</internetGateway>`)
}

func (f *fakeVPCs) attachGateway(call awsCall) awsReply {
	g, ok := f.gateways[call.Form.Get("InternetGatewayId")]
	if !ok {
		return notFound("InvalidInternetGatewayID.NotFound", "internetGateway ID", call.Form.Get("InternetGatewayId"))
	}
	g.VPC = call.Form.Get("VpcId")
	return done("AttachInternetGateway")
}

func (f *fakeVPCs) detachGateway(call awsCall) awsReply {
	g, ok := f.gateways[call.Form.Get("InternetGatewayId")]
	if !ok {
		return notFound("InvalidInternetGatewayID.NotFound", "internetGateway ID", call.Form.Get("InternetGatewayId"))
	}
	g.VPC = ""
	return done("DetachInternetGateway")
}

func (f *fakeVPCs) deleteGateway(call awsCall) awsReply {
	id := call.Form.Get("InternetGatewayId")
	if _, ok := f.gateways[id]; !ok {
		return notFound("InvalidInternetGatewayID.NotFound", "internetGateway ID", id)
	}
	delete(f.gateways, id)
	return done("DeleteInternetGateway")
}

func (f *fakeVPCs) tableXML(t *fakeTable) string {
	var b strings.Builder
	fmt.Fprintf(&b, `<routeTableId>%s</routeTableId><vpcId>%s</vpcId><ownerId>534742592531</ownerId><routeSet>`, esc(t.ID), esc(t.VPC))
	for _, dest := range sortedKeys(t.Routes) {
		field := "destinationCidrBlock"
		if strings.HasPrefix(dest, "pl-") {
			field = "destinationPrefixListId"
		}
		fmt.Fprintf(&b, `<item><%[1]s>%[2]s</%[1]s><gatewayId>%[3]s</gatewayId><state>active</state></item>`, field, esc(dest), esc(t.Routes[dest]))
	}
	b.WriteString(`</routeSet><associationSet>`)
	if t.Main {
		fmt.Fprintf(&b, `<item><routeTableAssociationId>rtbassoc-main%s</routeTableAssociationId><routeTableId>%s</routeTableId><main>true</main></item>`, esc(t.ID), esc(t.ID))
	}
	for _, s := range t.Subnets {
		fmt.Fprintf(&b, `<item><routeTableAssociationId>rtbassoc-%s</routeTableAssociationId><routeTableId>%s</routeTableId><subnetId>%s</subnetId><main>false</main></item>`, esc(s), esc(t.ID), esc(s))
	}
	b.WriteString(`</associationSet>` + tagSet(t.Tags))
	return b.String()
}

func (f *fakeVPCs) describeTables(call awsCall) awsReply {
	want := filters(call.Form)
	var b strings.Builder
	for _, id := range sortedKeys(f.tables) {
		if t := f.tables[id]; pass(want, "vpc-id", t.VPC) && matches(t.Tags, want) {
			b.WriteString(`<item>` + f.tableXML(t) + `</item>`)
		}
	}
	return reply("DescribeRouteTables", `<routeTableSet>`+b.String()+`</routeTableSet>`)
}

func (f *fakeVPCs) createTable(call awsCall) awsReply {
	v, ok := f.vpcs[call.Form.Get("VpcId")]
	if !ok {
		return notFound("InvalidVpcID.NotFound", "vpc ID", call.Form.Get("VpcId"))
	}
	t := &fakeTable{ID: f.id("rtb"), VPC: v.ID, Routes: map[string]string{v.CIDR: "local"}, Tags: specTags(call.Form, "route-table")}
	f.tables[t.ID] = t
	return reply("CreateRouteTable", `<routeTable>`+f.tableXML(t)+`</routeTable>`)
}

func (f *fakeVPCs) createRoute(call awsCall) awsReply {
	t, ok := f.tables[call.Form.Get("RouteTableId")]
	if !ok {
		return notFound("InvalidRouteTableID.NotFound", "routeTable ID", call.Form.Get("RouteTableId"))
	}
	t.Routes[call.Form.Get("DestinationCidrBlock")] = call.Form.Get("GatewayId")
	return done("CreateRoute")
}

func (f *fakeVPCs) associate(call awsCall) awsReply {
	t, ok := f.tables[call.Form.Get("RouteTableId")]
	if !ok {
		return notFound("InvalidRouteTableID.NotFound", "routeTable ID", call.Form.Get("RouteTableId"))
	}
	subnet := call.Form.Get("SubnetId")
	t.Subnets = append(t.Subnets, subnet)
	return reply("AssociateRouteTable", fmt.Sprintf(`<associationId>rtbassoc-%s</associationId><associationState><state>associated</state></associationState>`, esc(subnet)))
}

func (f *fakeVPCs) deleteTable(call awsCall) awsReply {
	id := call.Form.Get("RouteTableId")
	if _, ok := f.tables[id]; !ok {
		return notFound("InvalidRouteTableID.NotFound", "routeTable ID", id)
	}
	delete(f.tables, id)
	return done("DeleteRouteTable")
}

func (f *fakeVPCs) describeZones(awsCall) awsReply {
	var b strings.Builder
	for _, z := range fakeZones() {
		fmt.Fprintf(&b, `<item><zoneName>%s</zoneName><zoneId>%s</zoneId><zoneState>available</zoneState><regionName>us-east-2</regionName><zoneType>availability-zone</zoneType></item>`, z[0], z[1])
	}
	return reply("DescribeAvailabilityZones", `<availabilityZoneInfo>`+b.String()+`</availabilityZoneInfo>`)
}

func (f *fakeVPCs) subnetXML(s *fakeSubnet) string {
	return fmt.Sprintf(`<subnetId>%s</subnetId><state>available</state><vpcId>%s</vpcId><cidrBlock>%s</cidrBlock><availabilityZone>%s</availabilityZone>`+
		`<availabilityZoneId>%s</availabilityZoneId><defaultForAz>false</defaultForAz><mapPublicIpOnLaunch>%t</mapPublicIpOnLaunch>%s`,
		esc(s.ID), esc(s.VPC), esc(s.CIDR), esc(s.Zone), esc(s.ZoneID), s.Public, tagSet(s.Tags))
}

func (f *fakeVPCs) describeSubnets(call awsCall) awsReply {
	want := filters(call.Form)
	var b strings.Builder
	for _, id := range sortedKeys(f.subnets) {
		if s := f.subnets[id]; pass(want, "vpc-id", s.VPC) && matches(s.Tags, want) {
			b.WriteString(`<item>` + f.subnetXML(s) + `</item>`)
		}
	}
	return reply("DescribeSubnets", `<subnetSet>`+b.String()+`</subnetSet>`)
}

func (f *fakeVPCs) createSubnet(call awsCall) awsReply {
	zone := call.Form.Get("AvailabilityZone")
	s := &fakeSubnet{ID: f.id("subnet"), VPC: call.Form.Get("VpcId"), Zone: zone, CIDR: call.Form.Get("CidrBlock"), Tags: specTags(call.Form, "subnet")}
	for _, z := range fakeZones() {
		if z[0] == zone {
			s.ZoneID = z[1]
		}
	}
	f.subnets[s.ID] = s
	return reply("CreateSubnet", `<subnet>`+f.subnetXML(s)+`</subnet>`)
}

func (f *fakeVPCs) modifySubnetAttribute(call awsCall) awsReply {
	s, ok := f.subnets[call.Form.Get("SubnetId")]
	if !ok {
		return notFound("InvalidSubnetID.NotFound", "subnet ID", call.Form.Get("SubnetId"))
	}
	s.Public = call.Form.Get("MapPublicIpOnLaunch.Value") == "true"
	return done("ModifySubnetAttribute")
}

// deleteSubnet refuses a held subnet with the DependencyViolation code
// DeleteVpc answered in the recording.
func (f *fakeVPCs) deleteSubnet(call awsCall) awsReply {
	id := call.Form.Get("SubnetId")
	if _, ok := f.subnets[id]; !ok {
		return notFound("InvalidSubnetID.NotFound", "subnet ID", id)
	}
	if f.held[id] {
		return dependencyViolation("subnet", id)
	}
	delete(f.subnets, id)
	for _, t := range f.tables {
		t.Subnets = slices.DeleteFunc(t.Subnets, func(s string) bool { return s == id })
	}
	return done("DeleteSubnet")
}

func (f *fakeVPCs) describeGroups(call awsCall) awsReply {
	want := filters(call.Form)
	var b strings.Builder
	for _, id := range sortedKeys(f.groups) {
		g := f.groups[id]
		if !pass(want, "vpc-id", g.VPC) || !pass(want, "group-name", g.Name) || !matches(g.Tags, want) {
			continue
		}
		fmt.Fprintf(&b, `<item><ownerId>534742592531</ownerId><groupId>%s</groupId><groupName>%s</groupName><vpcId>%s</vpcId><ipPermissions>`, esc(g.ID), esc(g.Name), esc(g.VPC))
		if len(g.Peers) > 0 {
			b.WriteString(`<item><ipProtocol>tcp</ipProtocol><fromPort>12122</fromPort><toPort>12122</toPort><groups>`)
			for _, p := range g.Peers {
				fmt.Fprintf(&b, `<item><userId>534742592531</userId><groupId>%s</groupId></item>`, esc(p))
			}
			b.WriteString(`</groups><ipRanges/></item>`)
		}
		b.WriteString(`</ipPermissions><ipPermissionsEgress><item><ipProtocol>-1</ipProtocol><groups/><ipRanges><item><cidrIp>0.0.0.0/0</cidrIp></item></ipRanges></item></ipPermissionsEgress>` +
			tagSet(g.Tags) + `</item>`)
	}
	return reply("DescribeSecurityGroups", `<securityGroupInfo>`+b.String()+`</securityGroupInfo>`)
}

func (f *fakeVPCs) createGroup(call awsCall) awsReply {
	g := &fakeGroup{ID: f.id("sg"), VPC: call.Form.Get("VpcId"), Name: call.Form.Get("GroupName"), Tags: specTags(call.Form, "security-group")}
	f.groups[g.ID] = g
	return reply("CreateSecurityGroup", fmt.Sprintf(`<return>true</return><groupId>%s</groupId>%s`, esc(g.ID), tagSet(g.Tags)))
}

// authorize admits the peer port from the groups the call names; the fleet
// asks for no other rule.
func (f *fakeVPCs) authorize(call awsCall) awsReply {
	g, ok := f.groups[call.Form.Get("GroupId")]
	if !ok {
		return notFound("InvalidGroup.NotFound", "security group", call.Form.Get("GroupId"))
	}
	if call.Form.Get("IpPermissions.1.FromPort") == "12122" && call.Form.Get("IpPermissions.1.IpProtocol") == "tcp" {
		for n := 1; call.Form.Has(fmt.Sprintf("IpPermissions.1.Groups.%d.GroupId", n)); n++ {
			g.Peers = append(g.Peers, call.Form.Get(fmt.Sprintf("IpPermissions.1.Groups.%d.GroupId", n)))
		}
	}
	return reply("AuthorizeSecurityGroupIngress", `<return>true</return>`)
}

func (f *fakeVPCs) deleteGroup(call awsCall) awsReply {
	id := call.Form.Get("GroupId")
	if _, ok := f.groups[id]; !ok {
		return notFound("InvalidGroup.NotFound", "security group", id)
	}
	delete(f.groups, id)
	return reply("DeleteSecurityGroup", fmt.Sprintf(`<groupId>%s</groupId><return>true</return>`, esc(id)))
}

func (f *fakeVPCs) endpointXML(e *fakeEndpoint) string {
	var b strings.Builder
	b.WriteString(`<routeTableIdSet>`)
	for _, t := range e.Tables {
		fmt.Fprintf(&b, `<item>%s</item>`, esc(t))
	}
	return fmt.Sprintf(`%s</routeTableIdSet><serviceName>%s</serviceName><state>available</state>%s<vpcEndpointId>%s</vpcEndpointId><vpcEndpointType>Gateway</vpcEndpointType><vpcId>%s</vpcId>`,
		b.String(), esc(e.Service), tagSet(e.Tags), esc(e.ID), esc(e.VPC))
}

func (f *fakeVPCs) describeEndpoints(call awsCall) awsReply {
	want := filters(call.Form)
	var b strings.Builder
	for _, id := range sortedKeys(f.endpoints) {
		e := f.endpoints[id]
		if pass(want, "vpc-id", e.VPC) && pass(want, "service-name", e.Service) && pass(want, "vpc-endpoint-type", "Gateway") && matches(e.Tags, want) {
			b.WriteString(`<item>` + f.endpointXML(e) + `</item>`)
		}
	}
	return reply("DescribeVpcEndpoints", `<vpcEndpointSet>`+b.String()+`</vpcEndpointSet>`)
}

func (f *fakeVPCs) createEndpoint(call awsCall) awsReply {
	e := &fakeEndpoint{
		ID: f.id("vpce"), VPC: call.Form.Get("VpcId"), Service: call.Form.Get("ServiceName"),
		Tables: list(call.Form, "RouteTableId"), Tags: specTags(call.Form, "vpc-endpoint"),
	}
	f.endpoints[e.ID] = e
	for _, t := range e.Tables {
		f.tables[t].Routes["pl-7ba54012"] = e.ID
	}
	return reply("CreateVpcEndpoint", `<vpcEndpoint>`+f.endpointXML(e)+`</vpcEndpoint>`)
}

func (f *fakeVPCs) deleteEndpoints(call awsCall) awsReply {
	var b strings.Builder
	for _, id := range list(call.Form, "VpcEndpointId") {
		e, ok := f.endpoints[id]
		if !ok {
			fmt.Fprintf(&b, `<item><error><code>InvalidVpcEndpoint.NotFound</code><message>user 534742592531 does not own a resource %s</message></error><resourceId>%s</resourceId></item>`, esc(id), esc(id))
			continue
		}
		for _, t := range e.Tables {
			if table, ok := f.tables[t]; ok {
				delete(table.Routes, "pl-7ba54012")
			}
		}
		delete(f.endpoints, id)
	}
	if b.Len() == 0 {
		return reply("DeleteVpcEndpoints", `<unsuccessful/>`)
	}
	return reply("DeleteVpcEndpoints", `<unsuccessful>`+b.String()+`</unsuccessful>`)
}
