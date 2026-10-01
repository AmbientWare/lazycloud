package compute

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

const (
	// launchLease outlasts one RunInstances call.
	launchLease = 2 * time.Minute
	// maxLaunchAttempts bounds launches that keep failing without an answer.
	maxLaunchAttempts = 5
	// rootVolumeGiB is each instance's encrypted root disk.
	rootVolumeGiB = 100
	cpuImage      = "resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
	gpuImage      = "resolve:ssm:/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-amazon-linux-2023/latest/ami-id"
)

// Tags on every instance the fleet launches.
const (
	tagFleet = "lazycloud:fleet"
	tagHost  = "lazycloud:host-id"
)

// launchTarget is where and as whom one host launches.
type launchTarget struct {
	scope           awsScope
	network         Network
	instanceProfile string
	// authorization and nodeRole are what a connection host launches
	// under; its identity proof must name nodeRole.
	authorization *uuid.UUID
	nodeRole      string
}

// Launch starts instances for requested hosts. Each launch is claimed with
// a lease and runs outside any transaction; RunInstances takes the host id
// as its client token, so a retry after a lost answer returns the same
// instance. A refusal for capacity or quota cools the offer down and fails
// the host, so the controller buys another offer on its next pass.
func (c *Compute) Launch(ctx context.Context, logger *slog.Logger) (int, error) {
	claimed, err := c.queries.ClaimLaunches(ctx, ClaimLaunchesParams{LeaseSeconds: launchLease.Seconds(), BatchSize: 10})
	if err != nil {
		return 0, fmt.Errorf("claim launches: %w", err)
	}
	launched := 0
	for _, h := range claimed {
		started, err := c.launch(ctx, logger, h)
		if err != nil {
			if ctx.Err() != nil {
				return launched, err
			}
			logger.ErrorContext(ctx, "launch host", "host_id", h.ID, "error", err)
			continue
		}
		if started {
			launched++
		}
	}
	return launched, nil
}

func (c *Compute) launch(ctx context.Context, logger *slog.Logger, h ClaimLaunchesRow) (bool, error) {
	target, err := c.launchTarget(ctx, h.ConnectionID, h.Region)
	if err != nil {
		return false, c.failLaunch(ctx, h, err.Error(), false)
	}
	release, err := c.TargetRelease(ctx)
	if err != nil {
		return false, c.failLaunch(ctx, h, "no agent release is published", false)
	}
	subnet, ok := subnetFor(target.network, h.AvailabilityZone, h.ID)
	if !ok {
		return false, c.failLaunch(ctx, h, fmt.Sprintf("no subnet in %s %s", h.Region, h.AvailabilityZone), false)
	}
	image := cpuImage
	if h.GpuCount > 0 {
		image = gpuImage
	}
	tags := []ec2types.Tag{
		{Key: aws.String(tagFleet), Value: aws.String(c.fleet.Name)},
		{Key: aws.String(tagHost), Value: aws.String(h.ID.String())},
		{Key: aws.String("Name"), Value: aws.String("lazycloud-" + h.ID.String()[:8])},
	}
	input := &ec2.RunInstancesInput{
		ClientToken:  aws.String(h.ID.String()),
		ImageId:      aws.String(image),
		InstanceType: ec2types.InstanceType(h.InstanceType),
		MinCount:     aws.Int32(1),
		MaxCount:     aws.Int32(1),
		SubnetId:     aws.String(subnet.ID),
		UserData:     aws.String(base64.StdEncoding.EncodeToString([]byte(c.bootstrap(h.ID, release)))),
		MetadataOptions: &ec2types.InstanceMetadataOptionsRequest{
			HttpTokens: ec2types.HttpTokensStateRequired, HttpPutResponseHopLimit: aws.Int32(1),
			HttpEndpoint: ec2types.InstanceMetadataEndpointStateEnabled,
		},
		BlockDeviceMappings: []ec2types.BlockDeviceMapping{{
			DeviceName: aws.String("/dev/xvda"),
			Ebs: &ec2types.EbsBlockDevice{
				VolumeSize: aws.Int32(rootVolumeGiB), VolumeType: ec2types.VolumeTypeGp3,
				Encrypted: aws.Bool(true), DeleteOnTermination: aws.Bool(true),
			},
		}},
		TagSpecifications: []ec2types.TagSpecification{
			{ResourceType: ec2types.ResourceTypeInstance, Tags: tags},
			{ResourceType: ec2types.ResourceTypeVolume, Tags: tags[:2]},
		},
		InstanceInitiatedShutdownBehavior: ec2types.ShutdownBehaviorTerminate,
	}
	if target.network.SecurityGroupID != "" {
		input.SecurityGroupIds = []string{target.network.SecurityGroupID}
	}
	if target.instanceProfile != "" {
		input.IamInstanceProfile = &ec2types.IamInstanceProfileSpecification{Name: aws.String(target.instanceProfile)}
	}
	if h.Market != nil && Market(*h.Market) == MarketSpot {
		input.InstanceMarketOptions = &ec2types.InstanceMarketOptionsRequest{
			MarketType: ec2types.MarketTypeSpot,
			SpotOptions: &ec2types.SpotMarketOptions{
				SpotInstanceType:             ec2types.SpotInstanceTypeOneTime,
				InstanceInterruptionBehavior: ec2types.InstanceInterruptionBehaviorTerminate,
			},
		}
	}
	out, err := c.aws().ec2(target.scope, h.Region).RunInstances(ctx, input)
	if err != nil {
		code := awsCode(err)
		switch {
		case capacityRefusal(code) || strings.HasPrefix(code, "InvalidParameter") || code == "UnauthorizedOperation":
			return false, c.failLaunch(ctx, h, describeAWSError(err), true)
		case h.LaunchAttempts >= maxLaunchAttempts:
			return false, c.failLaunch(ctx, h, describeAWSError(err), false)
		}
		return false, fmt.Errorf("run instance: %w", err)
	}
	if len(out.Instances) != 1 {
		return false, fmt.Errorf("run instance returned %d instances", len(out.Instances))
	}
	instance := out.Instances[0]
	zone := aws.ToString(instance.Placement.AvailabilityZone)
	zoneID := subnet.ZoneID
	if zone != subnet.Zone {
		zoneID = ""
	}
	n, err := c.queries.RecordLaunch(ctx, RecordLaunchParams{
		ID: h.ID, InstanceID: instance.InstanceId, AvailabilityZone: zone, AvailabilityZoneID: zoneID,
		AuthorizationID: target.authorization, NodeRoleArn: nilIfEmpty(target.nodeRole),
	})
	if err != nil {
		return false, fmt.Errorf("record launch: %w", err)
	}
	if n == 0 {
		// The host stopped being wanted while it launched.
		return false, c.terminate(ctx, target.scope, h.Region, aws.ToString(instance.InstanceId))
	}
	logger.InfoContext(ctx, "instance launched", "host_id", h.ID, "instance_id", aws.ToString(instance.InstanceId),
		"instance_type", h.InstanceType, "region", h.Region, "zone", zone, "market", deref(h.Market))
	return true, nil
}

// failLaunch fails a host that could not launch; cool also skips its offer
// until the cooldown ends.
func (c *Compute) failLaunch(ctx context.Context, h ClaimLaunchesRow, message string, cool bool) error {
	return inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		if cool && h.Market != nil {
			key := string(KindPlatform)
			if h.ConnectionID != nil {
				key = h.ConnectionID.String()
			}
			if err := q.InsertCooldown(ctx, InsertCooldownParams{
				ConnectionKey: key, Region: h.Region, InstanceType: h.InstanceType, Market: *h.Market,
				Seconds: c.fleet.CapacityCooldown.Seconds(), Reason: truncate(message),
			}); err != nil {
				return fmt.Errorf("insert cooldown: %w", err)
			}
		}
		if err := q.FailHost(ctx, FailHostParams{ID: h.ID, Failure: ptr(string(FailureUnknown)), Message: truncate("Launch failed: " + message)}); err != nil {
			return fmt.Errorf("fail host: %w", err)
		}
		return notifyChannel(ctx, tx, h.ID)
	})
}

// launchTarget resolves the credentials, network and instance profile for
// the platform or a connection in region.
func (c *Compute) launchTarget(ctx context.Context, connection *uuid.UUID, region string) (launchTarget, error) {
	if connection == nil {
		network, ok := c.fleet.Networks[region]
		if !ok {
			return launchTarget{}, fmt.Errorf("the platform has no network in %s", region)
		}
		return launchTarget{
			scope: awsScope{key: string(KindPlatform)}, network: network, instanceProfile: c.fleet.InstanceProfile,
			nodeRole: c.fleet.NodeRoleARN,
		}, nil
	}
	row, err := c.queries.ConnectionScope(ctx, *connection)
	if errors.Is(err, pgx.ErrNoRows) {
		return launchTarget{}, errors.New("the connection has no active authorization")
	}
	if err != nil {
		return launchTarget{}, fmt.Errorf("read connection scope: %w", err)
	}
	var networks map[string]Network
	if err := json.Unmarshal(row.Networks, &networks); err != nil {
		return launchTarget{}, fmt.Errorf("decode networks: %w", err)
	}
	network, ok := networks[region]
	if !ok {
		return launchTarget{}, fmt.Errorf("the connection has no network in %s", region)
	}
	scope := c.aws().assume(row.RoleArn, row.ExternalID, "lazycloud-fleet-"+connection.String()[:8], connection.String())
	if deref(row.NodeRoleArn) == "" || deref(row.NodeInstanceProfile) == "" {
		return launchTarget{}, errors.New("the connection's authorization names no node role")
	}
	return launchTarget{
		scope: scope, network: network, instanceProfile: deref(row.NodeInstanceProfile),
		authorization: &row.AuthorizationID, nodeRole: deref(row.NodeRoleArn),
	}, nil
}

// bootstrap is the instance's user data: install Docker if the image lacks
// it, then install the agent release as a service that enrolls with the
// instance's identity.
func (c *Compute) bootstrap(host uuid.UUID, release AgentRelease) string {
	gateway := strings.TrimRight(c.config.InstallURL, "/")
	args := []string{
		"--gateway", shellQuote(gateway), "--server", shellQuote(c.config.ServerAddress),
		"--cloud-host-id", shellQuote(host.String()), "--background",
		"--agent-version", shellQuote(release.Version),
	}
	if c.config.ServerPlaintext {
		args = append(args, "--server-plaintext")
	}
	if digest, ok := release.SHA256["amd64"]; ok {
		args = append(args, "--agent-sha256", shellQuote(digest))
	}
	return strings.Join([]string{
		"#!/bin/sh",
		"set -eu",
		"exec >>/var/log/lazycloud-bootstrap.log 2>&1",
		"command -v docker >/dev/null 2>&1 || dnf install -y docker",
		"systemctl enable --now docker",
		"curl -fsSL --retry 10 --retry-connrefused " + shellQuote(gateway+"/install/agent") + " | sh -s -- " + strings.Join(args, " "),
		"",
	}, "\n")
}

// terminate stops an instance in scope.
func (c *Compute) terminate(ctx context.Context, scope awsScope, region, instance string) error {
	_, err := c.aws().ec2(scope, region).TerminateInstances(ctx, &ec2.TerminateInstancesInput{InstanceIds: []string{instance}})
	if err != nil && awsCode(err) != "InvalidInstanceID.NotFound" {
		return fmt.Errorf("terminate %s: %w", instance, err)
	}
	return nil
}
