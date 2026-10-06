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
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// launchLease outlasts one RunInstances call.
	launchLease = 2 * time.Minute
	// maxLaunchAttempts bounds launches that keep failing without an answer.
	maxLaunchAttempts = 5
	// rootVolumeGiB is each instance's encrypted root disk; a host launched
	// able to hibernate adds its RAM for the hibernation image.
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
	// account is a connection host's AWS account, which the node image
	// is shared with; empty for platform hosts.
	account string
}

// Launch starts instances for requested hosts. Each launch is claimed with
// a lease and runs outside any transaction; RunInstances takes the host id
// as its client token, so a retry after a lost answer returns the same
// instance. A refusal for capacity or quota cools the offer down and fails
// the host, so the controller buys another offer on its next pass.
func (c *Compute) Launch(ctx context.Context, logger *slog.Logger) (int, error) {
	if c.config.ServedRelease != "" {
		release, err := c.TargetRelease(ctx)
		switch {
		case errors.Is(err, ErrNotFound):
		case err != nil:
			return 0, err
		case release.Version != c.config.ServedRelease:
			logger.InfoContext(ctx, "launches wait for the served agent release", "target", release.Version, "served", c.config.ServedRelease)
			return 0, nil
		}
	}
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

// launch launches h in the trace of the container it was bought for.
func (c *Compute) launch(ctx context.Context, logger *slog.Logger, h ClaimLaunchesRow) (bool, error) {
	ctx, span := c.hostSpan(ctx, h.ID, "compute.launch", attribute.String("lazycloud.instance_type", h.InstanceType),
		attribute.String("lazycloud.market", deref(h.Market)), attribute.String("lazycloud.region", h.Region))
	started, err := c.launchHost(ctx, logger, h)
	span.SetAttributes(attribute.Bool("lazycloud.started", started))
	telemetry.Fail(span, err)
	return started, err
}

// hostSpan starts a span of work on host in the trace of the container
// waiting longest for it, or in a trace of its own when none waits or the
// read fails. Without tracing it reads no trace.
func (c *Compute) hostSpan(ctx context.Context, host uuid.UUID, name string, attrs ...attribute.KeyValue) (context.Context, trace.Span) {
	var parent string
	if telemetry.Tracing(ctx) {
		parent, _ = c.queries.HostWaitTrace(ctx, &host)
	}
	return telemetry.StartFor(ctx, parent, name, trace.WithAttributes(append(attrs, telemetry.Host(host.String()))...))
}

func (c *Compute) launchHost(ctx context.Context, logger *slog.Logger, h ClaimLaunchesRow) (bool, error) {
	target, err := c.launchTarget(ctx, h.ConnectionID, h.Region)
	if err != nil {
		return false, c.failLaunch(ctx, h, err.Error(), false, false)
	}
	release, err := c.TargetRelease(ctx)
	if err != nil {
		return false, c.failLaunch(ctx, h, "no agent release is published", false, false)
	}
	subnet, ok := subnetFor(target.network, h.AvailabilityZone, h.ID)
	if !ok {
		return false, c.failLaunch(ctx, h, fmt.Sprintf("no subnet in %s %s", h.Region, h.AvailabilityZone), false, false)
	}
	image, ok := c.nodeImage(h.Region, h.GpuCount > 0)
	if !ok {
		return false, c.failLaunch(ctx, h, "no node image for "+h.Region, false, false)
	}
	if err := c.shareImage(ctx, target, h.Region, image); err != nil {
		if accessDenied(err) || strings.HasPrefix(awsCode(err), "InvalidAMI") {
			return false, c.failLaunch(ctx, h, "share node image: "+describeAWSError(err), false, false)
		}
		return false, fmt.Errorf("share node image: %w", err)
	}
	opts := launchOptionsFor(h)
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
				VolumeSize: aws.Int32(opts.rootGiB), VolumeType: ec2types.VolumeTypeGp3,
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
	if opts.hibernate {
		input.HibernationOptions = &ec2types.HibernationOptionsRequest{Configured: aws.Bool(true)}
	}
	if h.Market != nil && Market(*h.Market) == MarketSpot {
		spot := &ec2types.SpotMarketOptions{
			SpotInstanceType:             ec2types.SpotInstanceTypeOneTime,
			InstanceInterruptionBehavior: ec2types.InstanceInterruptionBehaviorTerminate,
		}
		if opts.persistent {
			// A reserve outlives a stop, so its request must too. EC2
			// relaunches a persistent request's instance when it ends, so
			// the request is tagged for cleanup and cancelled first.
			// EC2 refuses a persistent request whose instance would
			// terminate itself, so a guest shutdown stops it, and
			// reconcile retires a reserve that stopped unasked.
			spot.SpotInstanceType = ec2types.SpotInstanceTypePersistent
			spot.InstanceInterruptionBehavior = ec2types.InstanceInterruptionBehaviorStop
			input.InstanceInitiatedShutdownBehavior = ec2types.ShutdownBehaviorStop
			if opts.hibernate {
				spot.InstanceInterruptionBehavior = ec2types.InstanceInterruptionBehaviorHibernate
			}
			input.TagSpecifications = append(input.TagSpecifications,
				ec2types.TagSpecification{ResourceType: ec2types.ResourceTypeSpotInstancesRequest, Tags: tags[:2]})
		}
		input.InstanceMarketOptions = &ec2types.InstanceMarketOptionsRequest{MarketType: ec2types.MarketTypeSpot, SpotOptions: spot}
	}
	out, err := c.aws().ec2(target.scope, h.Region).RunInstances(ctx, input)
	if err != nil {
		code := awsCode(err)
		switch {
		case capacityRefusal(code) || strings.HasPrefix(code, "InvalidParameter") || code == "UnauthorizedOperation":
			return false, c.failLaunch(ctx, h, describeAWSError(err), true, quotaRefusal(code))
		case h.LaunchAttempts >= maxLaunchAttempts:
			return false, c.failLaunch(ctx, h, describeAWSError(err), false, false)
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
		SpotRequestID: persistentRequest(opts, instance), NodeImage: instance.ImageId, HibernationConfigured: opts.hibernate,
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

// launchOptions are the parts of a launch a reserve changes.
type launchOptions struct {
	rootGiB int32
	// hibernate launches the instance able to hibernate.
	hibernate bool
	// persistent buys Spot on a persistent request that stops instead of
	// terminating.
	persistent bool
}

// launchOptionsFor sizes a launch. A platform host bought for the reserve
// hibernates when it asks to and its catalog type can, with its root grown
// for the image. A Spot reserve keeps its request across stops. Serving and
// connection hosts launch with the plain root.
func launchOptionsFor(h ClaimLaunchesRow) launchOptions {
	opts := launchOptions{rootGiB: rootVolumeGiB}
	if h.ReserveMode == nil || HostKind(h.Kind) != KindPlatform {
		return opts
	}
	opts.persistent = h.Market != nil && Market(*h.Market) == MarketSpot
	t, _ := CatalogTypeNamed(h.InstanceType)
	opts.hibernate = ReserveMode(*h.ReserveMode) == ReserveHibernate && t.Hibernates
	opts.rootGiB = int32(t.RootGiB(opts.hibernate)) //nolint:gosec // At most 250: only types under 150 GiB of RAM hibernate.
	return opts
}

// persistentRequest is the Spot request a host must cancel before it
// terminates: a reserve's persistent one. A one-time request ends with its
// instance, and cancelling it is neither needed nor allowed.
func persistentRequest(opts launchOptions, instance ec2types.Instance) *string {
	if !opts.persistent {
		return nil
	}
	return nilIfEmpty(aws.ToString(instance.SpotInstanceRequestId))
}

// nodeImage is the AMI a host launches from: the baked image of its region
// when the fleet names them, for platform and connection hosts alike; a
// region the bake did not reach has none. A fleet without images (local
// development) launches the stock ones, which carry no gVisor.
func (c *Compute) nodeImage(region string, gpu bool) (string, bool) {
	images := c.fleet.Images
	if images == nil {
		if gpu {
			return gpuImage, true
		}
		return cpuImage, true
	}
	byRegion := images.CPU
	if gpu {
		byRegion = images.GPU
	}
	image, ok := byRegion[region]
	return image, ok && image != ""
}

// shareImage grants a connection's account launch permission on a baked
// image, with the platform's credentials, before the account launches it.
// Adding a permission the account holds already changes nothing, so every
// launch shares again and no record is kept. The images hold public
// software only; the agent and its credentials arrive at boot.
func (c *Compute) shareImage(ctx context.Context, target launchTarget, region, image string) error {
	if target.account == "" || strings.HasPrefix(image, "resolve:ssm:") {
		return nil
	}
	_, err := c.aws().ec2(awsScope{key: string(KindPlatform)}, region).ModifyImageAttribute(ctx, &ec2.ModifyImageAttributeInput{
		ImageId: aws.String(image),
		LaunchPermission: &ec2types.LaunchPermissionModifications{
			Add: []ec2types.LaunchPermission{{UserId: aws.String(target.account)}},
		},
	})
	if err != nil {
		return fmt.Errorf("modify image attribute: %w", err)
	}
	return nil
}

// failLaunch fails a host that could not launch; cool also skips its offer
// until the cooldown ends, and quota its class in the region for the
// platform.
func (c *Compute) failLaunch(ctx context.Context, h ClaimLaunchesRow, message string, cool, quota bool) error {
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
			if quota && h.ConnectionID == nil {
				if err := c.refuseQuota(ctx, q, h.Region, h.InstanceType, Market(*h.Market)); err != nil {
					return err
				}
			}
		}
		if _, err := q.FailHost(ctx, FailHostParams{
			ID: h.ID, FromPhase: string(PhaseRequested), Failure: ptr(string(FailureUnknown)), Message: truncate("Launch failed: " + message),
		}); err != nil {
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
		authorization: &row.AuthorizationID, nodeRole: deref(row.NodeRoleArn), account: accountOfRole(row.RoleArn),
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

// terminate ends an instance in scope. A Spot reserve's persistent
// request is cancelled first, or EC2 would launch a replacement.
func (c *Compute) terminate(ctx context.Context, scope awsScope, region, instance string) error {
	client := c.aws().ec2(scope, region)
	request, err := c.spotRequestOf(ctx, instance)
	if err != nil {
		return err
	}
	if request != "" {
		return endSpotRequest(ctx, client, request)
	}
	_, err = client.TerminateInstances(ctx, &ec2.TerminateInstancesInput{InstanceIds: []string{instance}})
	if err != nil && awsCode(err) != "InvalidInstanceID.NotFound" {
		return fmt.Errorf("terminate %s: %w", instance, err)
	}
	return nil
}
