package compute

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"time"

	"github.com/aws/aws-sdk-go-v2/config"
)

// LoadFleet reads the fleet configuration the server and scheduler share
// from getenv, with AWS credentials from the default chain when it uses AWS:
//
//	LAZYCLOUD_FLEET_NAME              tag on every launched instance (lazycloud)
//	LAZYCLOUD_FLEET_ACCOUNT_ID        platform AWS account
//	LAZYCLOUD_FLEET_NODE_ROLE_ARN     role platform instances run as
//	LAZYCLOUD_FLEET_INSTANCE_PROFILE  its instance profile
//	LAZYCLOUD_FLEET_NETWORKS          {"us-east-2": {"vpc_id", "security_group_id", "subnets": [{"id", "zone", "zone_id"}]}}
//	LAZYCLOUD_FLEET_IMAGES            {"cpu": {"us-east-2": "ami-..."}, "gpu": {...}}; unset uses stock Amazon Linux
//	LAZYCLOUD_FLEET_MAX_HOSTS         live cloud hosts per owner (20)
//	LAZYCLOUD_FLEET_IDLE_TIMEOUT      idle time before a host drains (5m)
//	LAZYCLOUD_FLEET_HEADROOM          idle hosts each market keeps (0)
//	LAZYCLOUD_AWS_PRINCIPAL_ARN       principal connection roles trust
//	LAZYCLOUD_AWS_ENDPOINT_EC2, _STS, _CLOUDFORMATION  service endpoint overrides
func LoadFleet(ctx context.Context, getenv func(string) string) (Fleet, error) {
	f := Fleet{
		Name: getenv("LAZYCLOUD_FLEET_NAME"), AccountID: getenv("LAZYCLOUD_FLEET_ACCOUNT_ID"),
		NodeRoleARN: getenv("LAZYCLOUD_FLEET_NODE_ROLE_ARN"), InstanceProfile: getenv("LAZYCLOUD_FLEET_INSTANCE_PROFILE"),
		PrincipalARN: getenv("LAZYCLOUD_AWS_PRINCIPAL_ARN"),
		Endpoints: Endpoints{
			EC2: getenv("LAZYCLOUD_AWS_ENDPOINT_EC2"), STS: getenv("LAZYCLOUD_AWS_ENDPOINT_STS"),
			CloudFormation: getenv("LAZYCLOUD_AWS_ENDPOINT_CLOUDFORMATION"),
		},
	}
	if raw := getenv("LAZYCLOUD_FLEET_NETWORKS"); raw != "" {
		if err := json.Unmarshal([]byte(raw), &f.Networks); err != nil {
			return Fleet{}, fmt.Errorf("LAZYCLOUD_FLEET_NETWORKS: %w", err)
		}
	}
	if raw := getenv("LAZYCLOUD_FLEET_IMAGES"); raw != "" {
		f.Images = &NodeImages{}
		if err := json.Unmarshal([]byte(raw), f.Images); err != nil {
			return Fleet{}, fmt.Errorf("LAZYCLOUD_FLEET_IMAGES: %w", err)
		}
	}
	var err error
	if f.MaxHosts, err = intEnv(getenv, "LAZYCLOUD_FLEET_MAX_HOSTS"); err != nil {
		return Fleet{}, err
	}
	if f.HeadroomFloor, err = intEnv(getenv, "LAZYCLOUD_FLEET_HEADROOM"); err != nil {
		return Fleet{}, err
	}
	if raw := getenv("LAZYCLOUD_FLEET_IDLE_TIMEOUT"); raw != "" {
		if f.IdleTimeout, err = time.ParseDuration(raw); err != nil {
			return Fleet{}, fmt.Errorf("LAZYCLOUD_FLEET_IDLE_TIMEOUT: %w", err)
		}
	}
	// Without networks or a principal the platform makes no AWS call, so
	// it reads no AWS credentials either.
	if len(f.Networks) == 0 && f.PrincipalARN == "" {
		return f, nil
	}
	if f.AWS, err = config.LoadDefaultConfig(ctx); err != nil {
		return Fleet{}, fmt.Errorf("load AWS configuration: %w", err)
	}
	if f.AWS.Region == "" {
		f.AWS.Region = connectionRegion
	}
	return f, nil
}

func intEnv(getenv func(string) string, key string) (int, error) {
	raw := getenv(key)
	if raw == "" {
		return 0, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 0 {
		return 0, fmt.Errorf("%s must be a non-negative integer", key)
	}
	return n, nil
}
