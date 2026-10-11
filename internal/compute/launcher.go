package compute

import (
	"cmp"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// launchLease outlasts one pool's RunInstances call; each move to a next
	// pool renews it.
	launchLease = 2 * time.Minute
	// maxLaunchAttempts bounds launches that keep failing without an answer.
	maxLaunchAttempts = 5
	// maxLaunchPools bounds the pools one launch tries while EC2 refuses
	// them for capacity, quota or price.
	maxLaunchPools = 3
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
// a lease and runs outside any transaction; RunInstances takes the host id,
// numbered by each refused pool the launch moved past, as its client token,
// so a retry after a lost answer returns the same instance. A refusal for capacity, quota or price cools the
// refused pool and moves the host to the next ranked pool that still holds
// what it was bought for, up to maxLaunchPools; a host left without one
// fails, so the planner buys again on its next pass.
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
	pools := poolInputs{}
	for _, h := range claimed {
		started, err := c.launch(ctx, logger, h, pools)
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

// launch launches h in the trace of the container it was bought for,
// moving it on through refused pools.
func (c *Compute) launch(ctx context.Context, logger *slog.Logger, h ClaimLaunchesRow, pools poolInputs) (bool, error) {
	ctx, span := c.hostSpan(ctx, h.ID, "compute.launch", attribute.String("lazycloud.instance_type", h.InstanceType),
		attribute.String("lazycloud.market", deref(h.Market)), attribute.String("lazycloud.region", h.Region))
	started, err := c.launchPools(ctx, logger, h, pools)
	span.SetAttributes(attribute.Bool("lazycloud.started", started))
	telemetry.Fail(span, err)
	return started, err
}

// launchPools launches h in its pool, then in each next pool while EC2
// refuses the last for capacity, quota or price, up to maxLaunchPools.
func (c *Compute) launchPools(ctx context.Context, logger *slog.Logger, h ClaimLaunchesRow, pools poolInputs) (bool, error) {
	for {
		started, refusal, err := c.launchHost(ctx, logger, h)
		if refusal == nil {
			return started, err
		}
		cool := refusal.cooldown()
		var next FleetOffer
		var ok bool
		if h.LaunchPools+1 < maxLaunchPools {
			if next, ok, err = c.nextPool(ctx, h, pools, cool); err != nil {
				return false, err
			}
		} else {
			pools.cool(c, h, cool)
		}
		if !ok {
			return false, c.failLaunch(ctx, h, refusal.message(), &cool)
		}
		moved, err := c.movePool(ctx, h, next, refusal.message(), cool)
		if err != nil || !moved {
			return false, err
		}
		logger.InfoContext(ctx, "launch moved to the next pool", "host_id", h.ID,
			"refused", h.Region+"/"+refusal.zoneID+"/"+h.InstanceType, "next", next.Key(), "reason", refusal.message())
		pools.moved(h, next)
		h.Region, h.AvailabilityZone, h.InstanceType = next.Region, next.Zone, next.Type.Name
		h.LaunchPools++
	}
}

// poolRefusal is EC2 refusing a launch in one pool for capacity, quota or
// price, and the zone the launch tried.
type poolRefusal struct {
	err    error
	zoneID string
}

func (r *poolRefusal) message() string { return describeAWSError(r.err) }

// cooldown is how far the refusal cools its pool: the zone for capacity or
// price, the region and quota class for quota.
func (r *poolRefusal) cooldown() poolCooldown {
	if quotaRefusal(awsCode(r.err)) {
		return poolCooldown{quota: true}
	}
	return poolCooldown{zoneID: r.zoneID}
}

// poolCooldown is how far a refusal cools a host's type and market: in the
// zone zoneID, or in its whole region when zoneID is empty, and on a quota
// refusal the platform's quota class in the region.
type poolCooldown struct {
	zoneID string
	quota  bool
}

// poolInputs are one launch pass's offer inputs by owner, read at the
// owner's first refusal and kept current with each refusal and move after
// it.
type poolInputs map[string]*OfferInputs

// ownerKey names the owner of h's cooldowns: the platform or a connection.
func ownerKey(h ClaimLaunchesRow) string {
	if h.ConnectionID != nil {
		return h.ConnectionID.String()
	}
	return ownerPlatform
}

// cool records the refusal of h's pool in the pass's inputs as coolPool
// records it in the database, and frees the vCPUs h counted against the
// pool's quota.
func (p poolInputs) cool(c *Compute, h ClaimLaunchesRow, cool poolCooldown) {
	in, ok := p[ownerKey(h)]
	if !ok || h.Market == nil {
		return
	}
	market := Market(*h.Market)
	in.Cooldowns = append(in.Cooldowns, OfferCooldown{
		Region: h.Region, ZoneID: cool.zoneID, InstanceType: h.InstanceType, Market: market,
		Until: in.Now.Add(c.fleet.CapacityCooldown), Quota: cool.quota && h.ConnectionID == nil,
	})
	if t, known := CatalogTypeNamed(h.InstanceType); known && in.QuotaUsed != nil {
		class, _ := QuotaClassOf(t.Name)
		in.QuotaUsed[QuotaKey{Region: h.Region, Class: class, Market: market}] -= t.VCPUs()
	}
}

// moved counts h, moved to next, against next's quota.
func (p poolInputs) moved(h ClaimLaunchesRow, next FleetOffer) {
	if in, ok := p[ownerKey(h)]; ok && in.QuotaUsed != nil {
		in.QuotaUsed[next.Quota] += next.Type.VCPUs()
	}
}

// inputs reads the offer inputs of h's owner once a pass, as the planner
// reads them: cooldowns, quotas and the vCPUs live hosts count against
// them, and the memory each type reported.
func (p poolInputs) inputs(ctx context.Context, c *Compute, h ClaimLaunchesRow) (*OfferInputs, error) {
	owner := ownerKey(h)
	if in, ok := p[owner]; ok {
		return in, nil
	}
	now := time.Now()
	in := &OfferInputs{Now: now, Catalog: FleetCatalog(), ReportedMemory: map[string]int64{}}
	rows, err := c.queries.PlannerHosts(ctx)
	if err != nil {
		return nil, fmt.Errorf("read fleet hosts: %w", err)
	}
	var platform []FleetHost
	for _, row := range rows {
		if row.SessionEpoch > 0 && row.InstanceType != "" {
			if seen, ok := in.ReportedMemory[row.InstanceType]; !ok || row.MemoryBytes < seen {
				in.ReportedMemory[row.InstanceType] = row.MemoryBytes
			}
		}
		if HostKind(row.Kind) == KindPlatform {
			platform = append(platform, fleetHostOf(row, now, nil))
		}
	}
	if h.ConnectionID == nil {
		in.Networks = c.fleet.Networks
		if in.Rates, err = billing.FleetComputeRates(now); err != nil {
			return nil, fmt.Errorf("read the fleet's compute rates: %w", err)
		}
		rooms, err := quotaRooms(ctx, c.queries, now)
		if err != nil {
			return nil, err
		}
		in.Quotas, in.QuotaUsed = vcpuQuotas(rooms), QuotaUse(platform, in.Catalog)
	} else {
		row, err := c.queries.ConnectionScope(ctx, *h.ConnectionID)
		if err != nil {
			return nil, fmt.Errorf("read connection scope: %w", err)
		}
		if err := json.Unmarshal(row.Networks, &in.Networks); err != nil {
			return nil, fmt.Errorf("decode networks: %w", err)
		}
		in.OwnerPays = true
	}
	if in.Spot, err = readSpotPrices(ctx, c.queries); err != nil {
		return nil, err
	}
	if in.ZoneTypes, err = readZoneOfferings(ctx, c.queries); err != nil {
		return nil, err
	}
	cooldowns, err := c.queries.PlannerCooldowns(ctx)
	if err != nil {
		return nil, fmt.Errorf("read cooldowns: %w", err)
	}
	in.Cooldowns = offerCooldowns(cooldowns, owner)
	p[owner] = in
	return in, nil
}

// nextPool cools h's refused pool and returns the pool its launch moves
// to, as fallbackPool picks it from the owner's current offer inputs.
func (c *Compute) nextPool(ctx context.Context, h ClaimLaunchesRow, pools poolInputs, cool poolCooldown) (FleetOffer, bool, error) {
	if h.Market == nil {
		return FleetOffer{}, false, nil
	}
	in, err := pools.inputs(ctx, c, h)
	if err != nil {
		return FleetOffer{}, false, err
	}
	pools.cool(c, h, cool)
	waiters, err := c.queries.HostWaiters(ctx, &h.ID)
	if err != nil {
		return FleetOffer{}, false, fmt.Errorf("read the containers waiting for the host: %w", err)
	}
	next, ok := fallbackPool(c.policy(), h, waiters, *in, func(region string, gpu bool) bool {
		_, imaged := c.nodeImage(region, gpu)
		return imaged
	})
	return next, ok, nil
}

// fallbackPool is the cheapest pool, of those not refused and with a node
// image, that still holds what h was bought for: what the planner bought it
// to hold, or without that its capacity, its GPU model in its market, its
// reserve's sleep mode, and the placement each container waiting for it
// asks for.
// Pools are priced as the planner buys: a reserve by what it costs to keep
// stopped, any other host by what it costs to serve. A rightsize moves only
// to a pool that costs less than the host it replaces.
func fallbackPool(p Policy, h ClaimLaunchesRow, waiters []HostWaitersRow, in OfferInputs, imaged func(region string, gpu bool) bool) (FleetOffer, bool) {
	if h.Replaces != nil && h.ReplacesHourlyMicros == nil {
		return FleetOffer{}, false
	}
	market := Market(*h.Market)
	need := Requirement{CPUMillis: h.CpuMillis, MemoryBytes: h.MemoryBytes, GPUCount: int(h.GpuCount), Preemptible: market == MarketSpot}
	if h.HoldsCpuMillis != nil && h.HoldsMemoryBytes != nil {
		need.CPUMillis, need.MemoryBytes = *h.HoldsCpuMillis, *h.HoldsMemoryBytes
	}
	if h.GpuType != "" {
		need.GPUs = []string{h.GpuType}
	}
	for _, w := range waiters {
		need.Preemptible = need.Preemptible || w.Preemptible
		need.Region, need.Zone = cmp.Or(need.Region, w.Region), cmp.Or(need.Zone, w.Zone)
	}
	hibernate := h.ReserveMode != nil && ReserveMode(*h.ReserveMode) == ReserveHibernate
	offers := slices.DeleteFunc(RankOffers(p, need, h.ReserveMode != nil, in), func(o FleetOffer) bool {
		return o.Market != market || o.Type.GPU != h.GpuType || o.Hibernate != hibernate || !imaged(o.Region, o.Type.GPUCount > 0) ||
			h.Replaces != nil && o.HourlyMicros >= *h.ReplacesHourlyMicros ||
			slices.ContainsFunc(waiters, func(w HostWaitersRow) bool {
				return (w.Region != "" && ProductRegion(o.Region) != w.Region) || (w.Zone != "" && o.Zone != w.Zone && o.ZoneID != w.Zone)
			})
	})
	if len(offers) == 0 {
		return FleetOffer{}, false
	}
	cost := servingCost(p)
	if h.ReserveMode != nil {
		cost = reserveCost(p)
	}
	return slices.MinFunc(offers, func(a, b FleetOffer) int { return cmp.Compare(cost(a), cost(b)) }), true
}

// movePool cools h's refused pool and moves h to next in one transaction,
// renewing its lease. It reports false when h stopped being requested or
// another launcher moved it on.
func (c *Compute) movePool(ctx context.Context, h ClaimLaunchesRow, next FleetOffer, message string, cool poolCooldown) (bool, error) {
	moved := false
	err := inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		if err := c.coolPool(ctx, q, h, message, cool); err != nil {
			return err
		}
		n, err := q.MoveLaunchPool(ctx, MoveLaunchPoolParams{
			ID: h.ID, LaunchPools: h.LaunchPools, InstanceType: next.Type.Name, Region: next.Region,
			AvailabilityZone: next.Zone, AvailabilityZoneID: next.ZoneID, HourlyMicros: &next.HourlyMicros,
			CpuMillis: next.Usable.CPUMillis, MemoryBytes: next.Usable.MemoryBytes,
			LeaseSeconds: launchLease.Seconds(),
		})
		if err != nil {
			return fmt.Errorf("move launch pool: %w", err)
		}
		moved = n == 1
		return nil
	})
	return moved, err
}

// hostSpan starts a span of work on host in the trace of the container
// waiting longest for it, or in a trace of its own when none waits or the
// read fails. Without tracing it reads no trace.
func (c *Compute) hostSpan(ctx context.Context, host uuid.UUID, name string, attrs ...attribute.KeyValue) (context.Context, trace.Span) {
	var parent string
	if trace.SpanContextFromContext(ctx).IsValid() {
		parent, _ = c.queries.HostWaitTrace(ctx, &host)
	}
	return telemetry.StartFor(ctx, parent, name, trace.WithAttributes(append(attrs, telemetry.Host(host.String()))...))
}

// launchHost launches h in its pool. A refusal of the pool for capacity,
// quota or price is returned for the caller to move on; every other
// outcome is settled here.
func (c *Compute) launchHost(ctx context.Context, logger *slog.Logger, h ClaimLaunchesRow) (bool, *poolRefusal, error) {
	target, err := c.launchTarget(ctx, h.ConnectionID, h.Region)
	if err != nil {
		return false, nil, c.failLaunch(ctx, h, err.Error(), nil)
	}
	release, err := c.TargetRelease(ctx)
	if err != nil {
		return false, nil, c.failLaunch(ctx, h, "no agent release is published", nil)
	}
	subnet, ok := subnetFor(target.network, h.AvailabilityZone, h.ID)
	if !ok {
		return false, nil, c.failLaunch(ctx, h, fmt.Sprintf("no subnet in %s %s", h.Region, h.AvailabilityZone), nil)
	}
	image, ok := c.nodeImage(h.Region, h.GpuCount > 0)
	if !ok {
		return false, nil, c.failLaunch(ctx, h, "no node image for "+h.Region, nil)
	}
	if err := c.shareImage(ctx, target, h.Region, image); err != nil {
		if accessDenied(err) || strings.HasPrefix(awsCode(err), "InvalidAMI") {
			return false, nil, c.failLaunch(ctx, h, "share node image: "+describeAWSError(err), nil)
		}
		return false, nil, fmt.Errorf("share node image: %w", err)
	}
	opts := launchOptionsFor(h)
	tags := []ec2types.Tag{
		{Key: aws.String(tagFleet), Value: aws.String(c.fleet.Name)},
		{Key: aws.String(tagHost), Value: aws.String(h.ID.String())},
		{Key: aws.String("Name"), Value: aws.String("lazycloud-" + h.ID.String()[:8])},
	}
	input := &ec2.RunInstancesInput{
		ClientToken:  aws.String(launchToken(h)),
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
		BlockDeviceMappings: blockDevices(opts),
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
	out, err := c.aws().ec2(target.scope, h.Region).RunInstances(ctx, input, func(o *ec2.Options) {
		o.Retryer = poolRefusalFinal{o.Retryer}
	})
	if err != nil {
		code := awsCode(err)
		switch {
		case capacityRefusal(code):
			return false, &poolRefusal{err: fmt.Errorf("run instance: %w", err), zoneID: subnet.ZoneID}, nil
		case strings.HasPrefix(code, "InvalidParameter") || code == "UnauthorizedOperation":
			return false, nil, c.failLaunch(ctx, h, describeAWSError(err), &poolCooldown{})
		case h.LaunchAttempts >= maxLaunchAttempts:
			return false, nil, c.failLaunch(ctx, h, describeAWSError(err), nil)
		}
		return false, nil, fmt.Errorf("run instance: %w", err)
	}
	if len(out.Instances) != 1 {
		return false, nil, fmt.Errorf("run instance returned %d instances", len(out.Instances))
	}
	instance := out.Instances[0]
	zone := aws.ToString(instance.Placement.AvailabilityZone)
	zoneID := subnet.ZoneID
	if zone != subnet.Zone {
		zoneID = ""
	}
	n, err := c.queries.RecordLaunch(ctx, RecordLaunchParams{
		ID: h.ID, LaunchPools: h.LaunchPools, InstanceID: instance.InstanceId, AvailabilityZone: zone, AvailabilityZoneID: zoneID,
		AuthorizationID: target.authorization, NodeRoleArn: nilIfEmpty(target.nodeRole),
		SpotRequestID: persistentRequest(opts, instance), NodeImage: instance.ImageId, HibernationConfigured: opts.hibernate,
	})
	if err != nil {
		return false, nil, fmt.Errorf("record launch: %w", err)
	}
	if n == 0 {
		// The host stopped being wanted while it launched, or another
		// launcher moved it to another pool.
		return false, nil, c.terminate(ctx, target.scope, h.Region, aws.ToString(instance.InstanceId))
	}
	logger.InfoContext(ctx, "instance launched", "host_id", h.ID, "instance_id", aws.ToString(instance.InstanceId),
		"instance_type", h.InstanceType, "region", h.Region, "zone", zone, "market", deref(h.Market))
	return true, nil, nil
}

// launchToken is the client token of h's launch in its current pool: the
// host id in the pool it was bought from, then numbered by the pools it
// moved past. EC2 refuses a token reused with other parameters.
func launchToken(h ClaimLaunchesRow) string {
	if h.LaunchPools == 0 {
		return h.ID.String()
	}
	return fmt.Sprintf("%s-%d", h.ID, h.LaunchPools)
}

// poolRefusalFinal answers a launch's pool refusal at once instead of
// retrying it; EC2 sends some, such as InsufficientInstanceCapacity, as
// server faults, and the next pool is the retry.
type poolRefusalFinal struct{ aws.Retryer }

func (r poolRefusalFinal) IsErrorRetryable(err error) bool {
	return !capacityRefusal(awsCode(err)) && r.Retryer.IsErrorRetryable(err)
}

// launchOptions are the parts of a launch its type and a reserve change.
type launchOptions struct {
	rootGiB, rootMiBps int32
	// dataGiB is the EBS data volume holding disk copies; 0 on a type whose
	// instance store holds them.
	dataGiB int32
	// hibernate launches the instance able to hibernate.
	hibernate bool
	// persistent buys Spot on a persistent request that stops instead of
	// terminating.
	persistent bool
}

// launchOptionsFor sizes a launch. Its root has the throughput its type is
// priced with. A platform host bought for the reserve hibernates when it
// asks to and its catalog type can, with its root grown for the image. A
// Spot reserve keeps its request across stops. Serving and connection
// hosts launch with the plain root.
func launchOptionsFor(h ClaimLaunchesRow) launchOptions {
	t, _ := CatalogTypeNamed(h.InstanceType)
	opts := launchOptions{
		rootGiB: rootVolumeGiB, rootMiBps: int32(t.RootMiBps()), //nolint:gosec // At most buildMiBps.
		dataGiB: int32(t.DataVolumeGiB()), //nolint:gosec // At most a few TiB.
	}
	if h.ReserveMode == nil || HostKind(h.Kind) != KindPlatform {
		return opts
	}
	opts.persistent = h.Market != nil && Market(*h.Market) == MarketSpot
	opts.hibernate = ReserveMode(*h.ReserveMode) == ReserveHibernate && t.Hibernates
	opts.rootGiB = int32(t.RootGiB(opts.hibernate)) //nolint:gosec // At most 250: only types under 150 GiB of RAM hibernate.
	return opts
}

// dataVolumeDevice is the data volume's device name; the node image mounts
// the volume Amazon Linux links there at the disk engine's directory.
const dataVolumeDevice = "/dev/sdf"

// blockDevices are the encrypted root and, on a type without instance
// store, the data volume.
func blockDevices(opts launchOptions) []ec2types.BlockDeviceMapping {
	devices := []ec2types.BlockDeviceMapping{{
		DeviceName: aws.String("/dev/xvda"),
		Ebs: &ec2types.EbsBlockDevice{
			VolumeSize: aws.Int32(opts.rootGiB), VolumeType: ec2types.VolumeTypeGp3,
			Throughput: aws.Int32(opts.rootMiBps),
			Encrypted:  aws.Bool(true), DeleteOnTermination: aws.Bool(true),
		},
	}}
	if opts.dataGiB > 0 {
		devices = append(devices, ec2types.BlockDeviceMapping{
			DeviceName: aws.String(dataVolumeDevice),
			Ebs: &ec2types.EbsBlockDevice{
				VolumeSize: aws.Int32(opts.dataGiB), VolumeType: ec2types.VolumeTypeGp3,
				Encrypted: aws.Bool(true), DeleteOnTermination: aws.Bool(true),
			},
		})
	}
	return devices
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

// failLaunch fails a host that could not launch; a cooldown, when given,
// also skips its pool until the cooldown ends.
func (c *Compute) failLaunch(ctx context.Context, h ClaimLaunchesRow, message string, cool *poolCooldown) error {
	return inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		if cool != nil {
			if err := c.coolPool(ctx, q, h, message, *cool); err != nil {
				return err
			}
		}
		if _, err := q.FailHost(ctx, FailHostParams{
			ID: h.ID, FromPhase: string(PhaseRequested), Failure: ptr(string(FailureUnknown)), Message: truncate("Launch failed: " + message),
		}); err != nil {
			return fmt.Errorf("fail host: %w", err)
		}
		if h.Replaces != nil && cool != nil {
			if err := q.RefuseRightsize(ctx, *h.Replaces); err != nil {
				return fmt.Errorf("record the refused rightsize: %w", err)
			}
		}
		return notifyChannel(ctx, tx, h.ID)
	})
}

// coolPool skips h's type and market in the zone or region cool names
// until the cooldown ends, and on a quota refusal its class in the region
// for the platform.
func (c *Compute) coolPool(ctx context.Context, q *Queries, h ClaimLaunchesRow, message string, cool poolCooldown) error {
	if h.Market == nil {
		return nil
	}
	if err := q.InsertCooldown(ctx, InsertCooldownParams{
		ConnectionKey: ownerKey(h), Region: h.Region, AvailabilityZoneID: cool.zoneID, InstanceType: h.InstanceType,
		Market: *h.Market, Seconds: c.fleet.CapacityCooldown.Seconds(), Reason: truncate(message),
	}); err != nil {
		return fmt.Errorf("insert cooldown: %w", err)
	}
	if cool.quota && h.ConnectionID == nil {
		return c.refuseQuota(ctx, q, h.Region, h.InstanceType, Market(*h.Market))
	}
	return nil
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
