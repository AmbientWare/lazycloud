package compute

import (
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/google/uuid"
)

// Fleet configures the AWS capacity of the platform and connected
// accounts. Without regions it launches nothing, which is how a local stack
// runs.
type Fleet struct {
	// Name tags every instance the fleet launches, in every account, so
	// reconciliation finds them.
	Name string
	// AWS holds the platform's credentials and default region.
	AWS aws.Config
	// AccountID is the platform account; its instances enroll as NodeRoleARN.
	AccountID string
	// PrincipalARN is the platform principal connection roles trust.
	PrincipalARN string
	// BucketPrefix starts the names of workspace buckets, which a connected
	// account holds as <prefix>-<account id>-*.
	BucketPrefix string
	// TemplateBucket is the platform bucket, in the platform's AWS region,
	// whose connection/ objects anyone may read. The server publishes the
	// connection template there for CloudFormation to read.
	TemplateBucket string
	// NodeRoleARN and InstanceProfile are what platform instances run as.
	NodeRoleARN     string
	InstanceProfile string
	// Regions are where the fleet runs, in every account: each account
	// gets a network in each, and adding one here is all expanding takes.
	Regions []string
	// StorageRegion holds the platform's registry and object store; hosts in
	// another region pay transfer on what they move to and from them.
	StorageRegion string
	// Images are the node images fleet hosts launch from. Nil launches
	// the stock Amazon Linux images, which carry no gVisor.
	Images *NodeImages
	// MaxHosts bounds the running and starting cloud hosts of each
	// account, which may hold as many stopped reserves besides.
	MaxHosts int
	// BootTimeout is how long a launched instance may take to enroll.
	BootTimeout time.Duration
	// CapacityCooldown is how long an offer that lacked capacity is skipped.
	CapacityCooldown time.Duration
	// DrainWait bounds how long a disconnect waits for the account's
	// instances to end while its role still works.
	DrainWait time.Duration
	// Endpoints override AWS service endpoints; tests point them at
	// recorded responses.
	Endpoints Endpoints
}

// Endpoints are AWS service base URLs; empty uses AWS.
type Endpoints struct {
	EC2            string
	STS            string
	CloudFormation string
	ServiceQuotas  string
	// S3 serves the template bucket and connected accounts' buckets,
	// path-style.
	S3 string
}

// Network is where instances launch in one region.
type Network struct {
	VPCID           string   `json:"vpc_id"`
	SecurityGroupID string   `json:"security_group_id"`
	Subnets         []Subnet `json:"subnets"`
}

// NodeImages maps region to AMI id for CPU and GPU hosts, as the node image
// bake publishes them.
type NodeImages struct {
	CPU map[string]string `json:"cpu"`
	GPU map[string]string `json:"gpu"`
}

// Subnet is one launchable subnet and its zone.
type Subnet struct {
	ID     string `json:"id"`
	Zone   string `json:"zone"`
	ZoneID string `json:"zone_id"`
}

func (f Fleet) withDefaults() Fleet {
	if f.Name == "" {
		f.Name = "lazycloud"
	}
	if f.MaxHosts == 0 {
		f.MaxHosts = 20
	}
	if f.BootTimeout == 0 {
		f.BootTimeout = 10 * time.Minute
	}
	if f.CapacityCooldown == 0 {
		f.CapacityCooldown = 10 * time.Minute
	}
	if f.DrainWait == 0 {
		f.DrainWait = time.Hour
	}
	return f
}

const gib = int64(1) << 30

// subnetFor picks the subnet to launch in: one in zone when set, otherwise
// the one after the host id's position, which spreads hosts over zones.
func subnetFor(network Network, zone string, host uuid.UUID) (Subnet, bool) {
	var candidates []Subnet
	for _, s := range network.Subnets {
		if zone == "" || strings.EqualFold(s.Zone, zone) {
			candidates = append(candidates, s)
		}
	}
	if len(candidates) == 0 {
		return Subnet{}, false
	}
	return candidates[int(host[15])%len(candidates)], true
}
