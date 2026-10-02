package compute

import (
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/google/uuid"
)

// Fleet configures the platform's AWS capacity. Without networks the
// platform launches nothing, which is how a local stack runs.
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
	// NodeRoleARN and InstanceProfile are what platform instances run as.
	NodeRoleARN     string
	InstanceProfile string
	// Networks are the platform's launchable regions.
	Networks map[string]Network
	// Images are the node images platform hosts launch from. Nil launches
	// the stock Amazon Linux images, which carry no gVisor.
	Images *NodeImages
	// MaxHosts bounds the running and starting cloud hosts of the platform
	// and of each connected account; the platform may hold as many
	// stopped reserves besides.
	MaxHosts int
	// IdleTimeout is how long a ready instance may run no container before
	// it leaves; a platform host also waits out the fleet policy's light
	// use window.
	IdleTimeout time.Duration
	// BootTimeout is how long a launched instance may take to enroll.
	BootTimeout time.Duration
	// CapacityCooldown is how long an offer that lacked capacity is skipped.
	CapacityCooldown time.Duration
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
	if f.IdleTimeout == 0 {
		f.IdleTimeout = 5 * time.Minute
	}
	if f.BootTimeout == 0 {
		f.BootTimeout = 10 * time.Minute
	}
	if f.CapacityCooldown == 0 {
		f.CapacityCooldown = 10 * time.Minute
	}
	return f
}

const gib = int64(1) << 30

// regionOrder is the purchase preference among the US regions.
func regionOrder() []string { return []string{"us-east-2", "us-west-1", "us-east-1", "us-west-2"} }

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
