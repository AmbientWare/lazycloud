package compute_test

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"maps"
	"os"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/servicequotas"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// acceptanceFleet tags everything the acceptance run creates; the sweep
// ends exactly what carries it.
const acceptanceFleet = "acceptance"

// acceptanceRegion is where the lifecycle runs launch.
const acceptanceRegion = "us-east-2"

// TestRealEC2Fleet checks against real EC2 what only EC2 can show: that
// the catalog, Spot price and quota reads match what EC2 answers, and that
// reserves launched through the launcher stop, hibernate, start and
// terminate through the actuator and reconciliation as the fleet expects.
// The hosts cannot reach a server, so the test stands in for their
// sessions' phase moves. It runs only with LAZYCLOUD_EC2_ACCEPTANCE_PROFILE
// naming the platform account's profile. Each reserve kind runs
// LAZYCLOUD_EC2_ACCEPTANCE_RUNS cycles (default 1), the GPU one
// LAZYCLOUD_EC2_ACCEPTANCE_GPU_RUNS, one instance per kind at a time; every
// instance and Spot request tagged for the acceptance fleet is ended when
// it finishes. With
// LAZYCLOUD_FLEET_SPOT_SNAPSHOT naming a file it writes the Spot prices it
// read there.
func TestRealEC2Fleet(t *testing.T) {
	profile := os.Getenv("LAZYCLOUD_EC2_ACCEPTANCE_PROFILE")
	if profile == "" {
		t.Skip("set LAZYCLOUD_EC2_ACCEPTANCE_PROFILE to the platform account's AWS profile")
	}
	cfg, err := config.LoadDefaultConfig(t.Context(), config.WithSharedConfigProfile(profile), config.WithRegion(acceptanceRegion))
	if err != nil {
		t.Fatal(err)
	}
	client := ec2.NewFromConfig(cfg)
	t.Cleanup(func() { sweepAcceptance(t, client) })
	networks := map[string]compute.Network{}
	for _, region := range []string{"us-east-2", "us-west-1", "us-east-1", "us-west-2"} {
		networks[region] = defaultNetwork(t, ec2.NewFromConfig(cfg, func(o *ec2.Options) { o.Region = region }))
	}

	t.Run("catalog", func(t *testing.T) {
		for region := range networks {
			checkCatalog(t, ec2.NewFromConfig(cfg, func(o *ec2.Options) { o.Region = region }), region)
		}
	})
	t.Run("prices_and_quotas", func(t *testing.T) {
		o := newOwners(t, compute.Config{Fleet: compute.Fleet{Name: acceptanceFleet, AWS: cfg, Networks: networks}})
		checkSpotPrices(t, o, networks)
		checkQuotas(t, o, cfg, networks)
	})

	runs := envCount(t, "LAZYCLOUD_EC2_ACCEPTANCE_RUNS", 1)
	gpuRuns := envCount(t, "LAZYCLOUD_EC2_ACCEPTANCE_GPU_RUNS", runs)
	timings := &lifecycleTimings{byPath: map[string][]time.Duration{}}
	t.Run("lifecycle", func(t *testing.T) {
		o := newOwners(t, compute.Config{
			InstallURL: "https://lazycloud.invalid", ServerAddress: "lazycloud.invalid:443",
			Fleet: compute.Fleet{Name: acceptanceFleet, AWS: cfg, MaxHosts: 3, Networks: map[string]compute.Network{acceptanceRegion: networks[acceptanceRegion]}},
		})
		publish(t, o.compute)
		for _, c := range []reserveCase{
			{instanceType: "m7i.large", market: compute.MarketOnDemand, mode: compute.ReserveHibernate},
			{instanceType: "m7i.large", market: compute.MarketSpot, mode: compute.ReserveHibernate},
			{instanceType: "g4dn.xlarge", market: compute.MarketOnDemand, mode: compute.ReserveStop, gpus: 1},
		} {
			cycles := runs
			if c.gpus > 0 {
				cycles = gpuRuns
			}
			t.Run(strings.ReplaceAll(c.name(), " ", "_"), func(t *testing.T) {
				t.Parallel()
				for range cycles {
					r, launched := launchReserve(t, o, client, networks[acceptanceRegion], c, timings)
					r.cycle(launched)
				}
			})
		}
	})
	for _, path := range slices.Sorted(maps.Keys(timings.byPath)) {
		d := slices.Sorted(slices.Values(timings.byPath[path]))
		t.Logf("%s: p50 %s over %d %v", path, d[len(d)/2], len(d), d)
	}
}

func envCount(t *testing.T, name string, fallback int) int {
	t.Helper()
	s := os.Getenv(name)
	if s == "" {
		return fallback
	}
	var n int
	if _, err := fmt.Sscan(s, &n); err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	return n
}

// lifecycleTimings collects the durations of every lifecycle run by path.
type lifecycleTimings struct {
	mu     sync.Mutex
	byPath map[string][]time.Duration
}

func (l *lifecycleTimings) add(path string, d time.Duration) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.byPath[path] = append(l.byPath[path], d.Round(time.Second))
}

// reserveCase is one reserve the lifecycle launches. Each case runs its
// cycles one after another, so at most one instance per case lives.
type reserveCase struct {
	instanceType string
	market       compute.Market
	mode         compute.ReserveMode
	gpus         int
}

func (c reserveCase) name() string {
	return c.instanceType + " " + string(c.market) + " " + string(c.mode)
}

// launchReserve asks for a reserve of the case and launches it through the
// launcher, in the next zone when one refuses the type for capacity (on
// 2026-10-02 us-east-2a and us-east-2c each refused m7i.large).
func launchReserve(t *testing.T, o owners, client *ec2.Client, network compute.Network, c reserveCase, timings *lifecycleTimings) (liveReserve, time.Time) {
	typ, ok := compute.CatalogTypeNamed(c.instanceType)
	if !ok {
		t.Fatalf("%s is not in the catalog", c.instanceType)
	}
	for _, subnet := range network.Subnets {
		host := scan[uuid.UUID](t, o.pool, `
insert into hosts (name, state, kind, provider, phase, cpu_millis, memory_bytes, gpu_type, gpu_count, region, availability_zone,
                   instance_type, market, reserve_mode)
values ('acceptance', 'offline', 'platform', 'aws', 'requested', $1, $2, $3, $4, $5, $6, $7, $8, $9)
returning id`, typ.CPUMillis, typ.MemoryBytes, typ.GPU, c.gpus, acceptanceRegion, subnet.Zone,
			c.instanceType, string(c.market), string(c.mode))
		launched := time.Now()
		for {
			if _, err := o.compute.Launch(t.Context(), discard()); err != nil {
				t.Fatalf("launch: %v", err)
			}
			var instance *string
			var phase, message string
			if err := o.pool.QueryRow(t.Context(), "select instance_id, phase, phase_message from hosts where id = $1", host).Scan(&instance, &phase, &message); err != nil {
				t.Fatal(err)
			}
			if instance != nil {
				return liveReserve{t: t, o: o, client: client, host: host, c: c, timings: timings}, launched
			}
			if phase == string(compute.PhaseFailed) {
				if !strings.Contains(message, "InsufficientInstanceCapacity") {
					t.Fatalf("%s launch failed: %s", c.name(), message)
				}
				t.Logf("%s in %s: %s", c.name(), subnet.Zone, message)
				break
			}
			if time.Since(launched) > time.Minute {
				t.Fatalf("%s not launched after a minute: %s %s", c.name(), phase, message)
			}
			time.Sleep(time.Second)
		}
	}
	t.Fatalf("no zone of %s launched %s", acceptanceRegion, c.name())
	return liveReserve{}, time.Time{}
}

// liveReserve drives one launched host through its lifecycle.
type liveReserve struct {
	t       *testing.T
	o       owners
	client  *ec2.Client
	host    uuid.UUID
	c       reserveCase
	timings *lifecycleTimings
}

func (r liveReserve) phase() string {
	return scan[string](r.t, r.o.pool, "select phase from hosts where id = $1", r.host)
}

// move stands in for the session or the planner.
func (r liveReserve) move(to compute.Phase) {
	run(r.t, r.o.pool, "update hosts set phase = $2, phase_at = now(), launch_lease_until = null where id = $1", r.host, string(to))
}

func (r liveReserve) act() {
	run(r.t, r.o.pool, "update hosts set launch_lease_until = null where id = $1", r.host)
	if _, err := r.o.compute.Actuate(r.t.Context(), discard()); err != nil {
		r.t.Logf("actuate: %v", err)
	}
}

func (r liveReserve) reconcile() {
	if err := r.o.compute.Reconcile(r.t.Context(), discard()); err != nil {
		r.t.Logf("reconcile: %v", err)
	}
}

func (r liveReserve) instance() ec2types.Instance {
	id := scan[string](r.t, r.o.pool, "select instance_id from hosts where id = $1", r.host)
	for start := time.Now(); ; time.Sleep(2 * time.Second) {
		out, err := r.client.DescribeInstances(r.t.Context(), &ec2.DescribeInstancesInput{InstanceIds: []string{id}})
		// EC2 may not list an instance for a few seconds after launching it.
		if strings.Contains(fmt.Sprint(err), "InvalidInstanceID.NotFound") && time.Since(start) < 30*time.Second {
			continue
		}
		if err != nil || len(out.Reservations) != 1 || len(out.Reservations[0].Instances) != 1 {
			r.t.Fatalf("describe %s: %v", id, err)
		}
		return out.Reservations[0].Instances[0]
	}
}

// until polls EC2 every 2 seconds, running step between polls, until the
// instance reaches state, and reports when it did.
func (r liveReserve) until(state ec2types.InstanceStateName, limit time.Duration, step func()) time.Time {
	start := time.Now()
	for {
		if i := r.instance(); i.State.Name == state {
			return time.Now()
		}
		if time.Since(start) > limit {
			r.t.Fatalf("%s: not %s after %s; host %s", r.c.name(), state, limit, r.phase())
		}
		time.Sleep(2 * time.Second)
		step()
	}
}

func (r liveReserve) cycle(launched time.Time) {
	t := r.t
	running := r.until(ec2types.InstanceStateNameRunning, 5*time.Minute, r.reconcile)
	r.timings.add(r.c.instanceType+" "+string(r.c.market)+": launch to running", running.Sub(launched))
	r.checkLaunch()
	for r.phase() != string(compute.PhaseBooting) {
		if time.Since(running) > 2*time.Minute {
			t.Fatalf("reconcile left a running host %s", r.phase())
		}
		time.Sleep(2 * time.Second)
		r.reconcile()
	}

	// The session moves a proven host to stopping. The actuator waits out
	// EC2's hibernation delay after the start.
	r.move(compute.PhaseStopping)
	deadline := time.Now().Add(6 * time.Minute)
	for !scan[bool](t, r.o.pool, "select stop_requested_at is not null from hosts where id = $1", r.host) {
		if time.Now().After(deadline) {
			t.Fatalf("stop not accepted; host %s: %s", r.phase(), scan[string](t, r.o.pool, "select phase_message from hosts where id = $1", r.host))
		}
		time.Sleep(2 * time.Second)
		r.act()
	}
	accepted := scan[time.Time](t, r.o.pool, "select stop_requested_at from hosts where id = $1", r.host)
	stopped := r.until(ec2types.InstanceStateNameStopped, 15*time.Minute, r.act)
	kind := "stop"
	if r.c.mode == compute.ReserveHibernate {
		kind = "hibernate"
	}
	r.timings.add(r.c.name()+": "+kind+" to stopped", stopped.Sub(accepted))
	for r.phase() != string(compute.PhaseStopped) {
		if time.Since(stopped) > time.Minute {
			t.Fatalf("actuator left a stopped instance's host %s", r.phase())
		}
		time.Sleep(2 * time.Second)
		r.act()
	}
	reason := aws.ToString(r.instance().StateReason.Code)
	evidence := scan[string](t, r.o.pool, "select image_evidence from hosts where id = $1", r.host)
	wantReason, wantEvidence := "Client.UserInitiatedShutdown", compute.EvidenceUnavailable
	if r.c.mode == compute.ReserveHibernate {
		wantReason, wantEvidence = "Client.UserInitiatedHibernate", compute.EvidenceSaved
	}
	if reason != wantReason || evidence != string(wantEvidence) {
		t.Errorf("%s stopped for %s with evidence %s, want %s and %s", r.c.name(), reason, evidence, wantReason, wantEvidence)
	}

	// The planner resumes it.
	r.move(compute.PhaseResuming)
	asked := time.Now()
	r.act()
	// Another reserve's actuator pass may hold this host's claim. EC2
	// answers IncorrectSpotRequestState for about 40 s after a Spot
	// reserve stops, which the actuator retries, and may then refuse the
	// start for Spot capacity, which retires the reserve.
	refused := false
	for scan[bool](t, r.o.pool, "select stop_requested_at is not null from hosts where id = $1", r.host) {
		if r.phase() == string(compute.PhaseTerminating) {
			refused = true
			t.Logf("%s: start refused, retired: %s", r.c.name(), scan[string](t, r.o.pool, "select phase_message from hosts where id = $1", r.host))
			break
		}
		if time.Since(asked) > 3*time.Minute {
			t.Fatalf("start not recorded; host %s", r.phase())
		}
		time.Sleep(2 * time.Second)
		r.act()
	}
	started := time.Now()
	if !refused {
		started = r.until(ec2types.InstanceStateNameRunning, 10*time.Minute, func() {})
		r.timings.add(r.c.name()+": start to running", started.Sub(asked))
		if r.c.mode == compute.ReserveHibernate {
			r.logResume()
		}
	}

	// The planner retires it.
	r.move(compute.PhaseTerminating)
	retired := time.Now()
	r.act()
	// A g4dn.xlarge stays shutting-down for minutes; EC2 stops billing it
	// there, so the run moves on.
	for s := r.instance().State.Name; s != ec2types.InstanceStateNameShuttingDown && s != ec2types.InstanceStateNameTerminated; s = r.instance().State.Name {
		if time.Since(retired) > 5*time.Minute {
			t.Fatalf("%s still %s after the terminate", r.c.name(), s)
		}
		time.Sleep(2 * time.Second)
		r.reconcile()
	}
	for r.phase() != string(compute.PhaseDeleted) {
		if time.Since(started) > 10*time.Minute {
			t.Fatalf("reconcile left a terminated host %s", r.phase())
		}
		time.Sleep(2 * time.Second)
		r.reconcile()
	}
	if r.c.market == compute.MarketSpot {
		r.checkRequestEnded()
	}
}

// checkLaunch checks the instance, its root volume and its Spot request
// carry what the launcher asked for.
func (r liveReserve) checkLaunch() {
	t := r.t
	i := r.instance()
	tags := map[string]string{}
	for _, tag := range i.Tags {
		tags[aws.ToString(tag.Key)] = aws.ToString(tag.Value)
	}
	if tags["lazycloud:fleet"] != acceptanceFleet || tags["lazycloud:host-id"] != r.host.String() || tags["Name"] == "" {
		t.Errorf("instance tags %v", tags)
	}
	typ, _ := compute.CatalogTypeNamed(r.c.instanceType)
	hibernates := r.c.mode == compute.ReserveHibernate
	root := int32(100)
	if hibernates {
		root += int32(typ.MemoryBytes / gib) //nolint:gosec // Catalog RAM is under 150 GiB.
	}
	configured := i.HibernationOptions != nil && aws.ToBool(i.HibernationOptions.Configured)
	recorded := scan[bool](t, r.o.pool, "select hibernation_configured from hosts where id = $1", r.host)
	if configured != hibernates || recorded != hibernates {
		t.Errorf("hibernation configured %t, recorded %t, want %t", configured, recorded, hibernates)
	}
	volumes, err := r.client.DescribeVolumes(t.Context(), &ec2.DescribeVolumesInput{Filters: []ec2types.Filter{
		{Name: aws.String("attachment.instance-id"), Values: []string{aws.ToString(i.InstanceId)}},
		{Name: aws.String("attachment.device"), Values: []string{aws.ToString(i.RootDeviceName)}},
	}})
	if err != nil || len(volumes.Volumes) != 1 {
		t.Fatalf("root volume of %s: %v", aws.ToString(i.InstanceId), err)
	}
	v := volumes.Volumes[0]
	vtags := map[string]string{}
	for _, tag := range v.Tags {
		vtags[aws.ToString(tag.Key)] = aws.ToString(tag.Value)
	}
	if aws.ToInt32(v.Size) != root || !aws.ToBool(v.Encrypted) || v.VolumeType != ec2types.VolumeTypeGp3 ||
		vtags["lazycloud:fleet"] != acceptanceFleet || vtags["lazycloud:host-id"] != r.host.String() {
		t.Errorf("root volume %d GiB encrypted %t %s tags %v, want %d GiB encrypted gp3 tagged", aws.ToInt32(v.Size),
			aws.ToBool(v.Encrypted), v.VolumeType, vtags, root)
	}
	spot := i.InstanceLifecycle == ec2types.InstanceLifecycleTypeSpot
	if spot != (r.c.market == compute.MarketSpot) {
		t.Errorf("instance lifecycle %q for a %s host", i.InstanceLifecycle, r.c.market)
	}
	request := scan[*string](t, r.o.pool, "select spot_request_id from hosts where id = $1", r.host)
	if !spot {
		if request != nil {
			t.Errorf("on-demand host recorded spot request %s", *request)
		}
		return
	}
	if request == nil || *request != aws.ToString(i.SpotInstanceRequestId) {
		t.Fatalf("recorded spot request %v, instance's %s", request, aws.ToString(i.SpotInstanceRequestId))
	}
	out, err := r.client.DescribeSpotInstanceRequests(t.Context(), &ec2.DescribeSpotInstanceRequestsInput{SpotInstanceRequestIds: []string{*request}})
	if err != nil || len(out.SpotInstanceRequests) != 1 {
		t.Fatalf("spot request %s: %v", *request, err)
	}
	sr := out.SpotInstanceRequests[0]
	rtags := map[string]string{}
	for _, tag := range sr.Tags {
		rtags[aws.ToString(tag.Key)] = aws.ToString(tag.Value)
	}
	if sr.Type != ec2types.SpotInstanceTypePersistent || sr.InstanceInterruptionBehavior != ec2types.InstanceInterruptionBehaviorStop ||
		rtags["lazycloud:fleet"] != acceptanceFleet || rtags["lazycloud:host-id"] != r.host.String() {
		t.Errorf("spot request %s %s interruption %s tags %v, want a tagged persistent request that stops", *request, sr.Type,
			sr.InstanceInterruptionBehavior, rtags)
	}
}

// checkRequestEnded checks a Spot reserve's terminate cancelled its
// persistent request and that nothing relaunched from it.
func (r liveReserve) checkRequestEnded() {
	t := r.t
	request := scan[string](t, r.o.pool, "select spot_request_id from hosts where id = $1", r.host)
	out, err := r.client.DescribeSpotInstanceRequests(t.Context(), &ec2.DescribeSpotInstanceRequestsInput{SpotInstanceRequestIds: []string{request}})
	if err != nil || len(out.SpotInstanceRequests) != 1 || out.SpotInstanceRequests[0].State != ec2types.SpotInstanceStateCancelled {
		t.Fatalf("spot request %s after retirement %v: %v, want cancelled", request, out, err)
	}
	time.Sleep(30 * time.Second)
	launched, err := r.client.DescribeInstances(t.Context(), &ec2.DescribeInstancesInput{Filters: []ec2types.Filter{
		{Name: aws.String("spot-instance-request-id"), Values: []string{request}},
		{Name: aws.String("instance-state-name"), Values: []string{"pending", "running", "stopping", "stopped"}},
	}})
	if err != nil || len(launched.Reservations) != 0 {
		t.Fatalf("instances of the cancelled request %s: %v %v, want none", request, launched.Reservations, err)
	}
}

// logResume logs the kernel's hibernation lines from the console.
func (r liveReserve) logResume() {
	time.Sleep(20 * time.Second)
	console, err := r.client.GetConsoleOutput(r.t.Context(), &ec2.GetConsoleOutputInput{InstanceId: r.instance().InstanceId, Latest: aws.Bool(true)})
	if err != nil {
		r.t.Logf("console: %v", err)
		return
	}
	text, err := base64.StdEncoding.DecodeString(aws.ToString(console.Output))
	if err != nil {
		r.t.Logf("console: %v", err)
		return
	}
	for line := range strings.SplitSeq(string(text), "\n") {
		if strings.Contains(line, "PM: ") && (strings.Contains(line, "hibernat") || strings.Contains(line, "restor") || strings.Contains(line, "Image")) {
			r.t.Logf("console: %s", strings.TrimSpace(line))
		}
	}
}

// defaultNetwork is the region's default subnets as a fleet network.
func defaultNetwork(t *testing.T, client *ec2.Client) compute.Network {
	t.Helper()
	subnets, err := client.DescribeSubnets(t.Context(), &ec2.DescribeSubnetsInput{
		Filters: []ec2types.Filter{{Name: aws.String("default-for-az"), Values: []string{"true"}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	network := compute.Network{}
	for _, s := range subnets.Subnets {
		network.VPCID = aws.ToString(s.VpcId)
		network.Subnets = append(network.Subnets, compute.Subnet{
			ID: aws.ToString(s.SubnetId), Zone: aws.ToString(s.AvailabilityZone), ZoneID: aws.ToString(s.AvailabilityZoneId),
		})
	}
	if len(network.Subnets) == 0 {
		t.Fatal("no default subnets")
	}
	return network
}

// checkCatalog checks EC2 sells each catalog type in exactly the regions
// the catalog prices it in, with the catalog's shape and hibernation flag.
func checkCatalog(t *testing.T, client *ec2.Client, region string) {
	var names []string
	for _, typ := range compute.FleetCatalog() {
		names = append(names, typ.Name)
	}
	sold := map[string]bool{}
	offerings := ec2.NewDescribeInstanceTypeOfferingsPaginator(client, &ec2.DescribeInstanceTypeOfferingsInput{
		LocationType: ec2types.LocationTypeRegion,
		Filters:      []ec2types.Filter{{Name: aws.String("instance-type"), Values: names}},
	})
	for offerings.HasMorePages() {
		page, err := offerings.NextPage(t.Context())
		if err != nil {
			t.Fatalf("%s offerings: %v", region, err)
		}
		for _, o := range page.InstanceTypeOfferings {
			sold[string(o.InstanceType)] = true
		}
	}
	described := map[string]ec2types.InstanceTypeInfo{}
	types := ec2.NewDescribeInstanceTypesPaginator(client, &ec2.DescribeInstanceTypesInput{
		Filters: []ec2types.Filter{{Name: aws.String("instance-type"), Values: names}},
	})
	for types.HasMorePages() {
		page, err := types.NextPage(t.Context())
		if err != nil {
			t.Fatalf("%s instance types: %v", region, err)
		}
		for _, it := range page.InstanceTypes {
			described[string(it.InstanceType)] = it
		}
	}
	hibernating := 0
	for _, typ := range compute.FleetCatalog() {
		_, priced := typ.OnDemandMicros(region)
		if priced != sold[typ.Name] {
			t.Errorf("%s in %s: priced %t, sold %t", typ.Name, region, priced, sold[typ.Name])
		}
		it, ok := described[typ.Name]
		if !priced || !ok {
			continue
		}
		vcpus := int64(aws.ToInt32(it.VCpuInfo.DefaultVCpus))
		memory := aws.ToInt64(it.MemoryInfo.SizeInMiB) << 20
		model, cards := "", 0
		if it.GpuInfo != nil {
			for _, g := range it.GpuInfo.Gpus {
				model, cards = aws.ToString(g.Name), cards+int(aws.ToInt32(g.Count))
			}
		}
		// The catalog names an A100 by its memory (A100-40, A100-80); EC2
		// names the card alone.
		if vcpus*1000 != typ.CPUMillis || memory != typ.MemoryBytes || !strings.HasPrefix(strings.ToLower(typ.GPU), strings.ToLower(model)) ||
			cards != typ.GPUCount {
			t.Errorf("%s in %s: EC2 has %d vCPU, %d MiB, %d %s; the catalog %d vCPU, %d MiB, %d %s", typ.Name, region,
				vcpus, memory>>20, cards, model, typ.VCPUs(), typ.MemoryBytes>>20, typ.GPUCount, typ.GPU)
		}
		if typ.Hibernates && !aws.ToBool(it.HibernationSupported) {
			t.Errorf("%s does not hibernate in %s", typ.Name, region)
		}
		if typ.Hibernates {
			hibernating++
		}
	}
	t.Logf("%s: %d of %d catalog types sold, %d hibernate", region, len(sold), len(names), hibernating)
}

// checkSpotPrices refreshes Spot prices through the fleet's reader and
// checks every catalog type sold in each region has a quote in every zone.
func checkSpotPrices(t *testing.T, o owners, networks map[string]compute.Network) {
	n, err := o.compute.RefreshSpotPrices(t.Context(), discard())
	if err != nil {
		t.Fatalf("refresh spot prices: %d stored, %v", n, err)
	}
	quotes, err := o.compute.SpotPrices(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	have := map[[3]string]bool{}
	for _, q := range quotes {
		have[[3]string{q.Region, q.ZoneID, q.InstanceType}] = true
	}
	missing := map[string][]string{}
	for region, network := range networks {
		for _, typ := range compute.FleetCatalog() {
			if _, sold := typ.OnDemandMicros(region); !sold {
				continue
			}
			for _, s := range network.Subnets {
				if !have[[3]string{region, s.ZoneID, typ.Name}] {
					missing[region] = append(missing[region], s.ZoneID+" "+typ.Name)
				}
			}
		}
	}
	t.Logf("%d Spot quotes; zones without a quote for a sold type: %v", len(quotes), missing)
	if path := os.Getenv("LAZYCLOUD_FLEET_SPOT_SNAPSHOT"); path != "" {
		type quote struct {
			Region       string `json:"region"`
			ZoneID       string `json:"zone_id"`
			InstanceType string `json:"instance_type"`
			HourlyMicros int64  `json:"hourly_micros"`
		}
		snapshot := struct {
			Source  string  `json:"source"`
			Fetched string  `json:"fetched"`
			Quotes  []quote `json:"quotes"`
		}{Source: "DescribeSpotPriceHistory, Linux/UNIX, latest quote per type and zone", Fetched: time.Now().UTC().Format(time.RFC3339)}
		for _, q := range quotes {
			snapshot.Quotes = append(snapshot.Quotes, quote{Region: q.Region, ZoneID: q.ZoneID, InstanceType: q.InstanceType, HourlyMicros: q.HourlyMicros})
		}
		raw, err := json.MarshalIndent(snapshot, "", " ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(raw, '\n'), 0o600); err != nil {
			t.Fatal(err)
		}
	}
}

// checkQuotas refreshes the EC2 vCPU quotas through the fleet's reader and
// checks each quota code names the class and market the fleet counts it
// against.
func checkQuotas(t *testing.T, o owners, cfg aws.Config, networks map[string]compute.Network) {
	n, err := o.compute.RefreshQuotas(t.Context(), discard())
	if err != nil || n != 6*len(networks) {
		t.Fatalf("refresh quotas: %d stored, %v; want %d", n, err, 6*len(networks))
	}
	rows, err := o.pool.Query(t.Context(), "select region || ' ' || quota_class || ' ' || market || ' ' || vcpus from fleet_quotas order by 1")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var stored []string
	for rows.Next() {
		var s string
		if err := rows.Scan(&s); err != nil {
			t.Fatal(err)
		}
		stored = append(stored, s)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	t.Logf("quotas: %v", stored)
	names := map[string]string{
		"L-1216C47A": "Running On-Demand Standard", "L-34B43A08": "All Standard (A, C, D, H, I, M, R, T, Z) Spot",
		"L-DB2E81BA": "Running On-Demand G and VT", "L-3819A6DF": "All G and VT Spot",
		"L-417A185B": "Running On-Demand P", "L-7212CCBC": "All P Spot",
	}
	quotas := servicequotas.NewFromConfig(cfg)
	for code, want := range names {
		out, err := quotas.GetServiceQuota(t.Context(), &servicequotas.GetServiceQuotaInput{ServiceCode: aws.String("ec2"), QuotaCode: aws.String(code)})
		if err != nil {
			t.Fatalf("quota %s: %v", code, err)
		}
		if name := aws.ToString(out.Quota.QuotaName); !strings.HasPrefix(name, want) {
			t.Errorf("quota %s is %q, want %q", code, name, want)
		}
	}
	for _, typ := range []string{"m7i.large", "c6a.2xlarge", "r6a.2xlarge", "g4dn.xlarge", "g6e.xlarge", "p5.48xlarge"} {
		class, _ := compute.QuotaClassOf(typ)
		t.Logf("%s counts against the %s quotas", typ, class)
	}
}

// sweepAcceptance cancels every acceptance Spot request still open, active
// or disabled, terminates every live acceptance instance in the
// acceptance region and checks none is left.
func sweepAcceptance(t *testing.T, client *ec2.Client) {
	ctx := context.WithoutCancel(t.Context())
	requests, err := client.DescribeSpotInstanceRequests(ctx, &ec2.DescribeSpotInstanceRequestsInput{Filters: []ec2types.Filter{
		{Name: aws.String("tag:lazycloud:fleet"), Values: []string{acceptanceFleet}},
		{Name: aws.String("state"), Values: []string{"open", "active", "disabled"}},
	}})
	if err != nil {
		t.Errorf("sweep: describe spot requests: %v", err)
	} else if len(requests.SpotInstanceRequests) > 0 {
		var ids []string
		for _, r := range requests.SpotInstanceRequests {
			ids = append(ids, aws.ToString(r.SpotInstanceRequestId))
		}
		t.Logf("sweep: cancelling %v", ids)
		if _, err := client.CancelSpotInstanceRequests(ctx, &ec2.CancelSpotInstanceRequestsInput{SpotInstanceRequestIds: ids}); err != nil {
			t.Errorf("sweep: cancel %v: %v", ids, err)
		}
	}
	live := func() []string {
		out, err := client.DescribeInstances(ctx, &ec2.DescribeInstancesInput{Filters: []ec2types.Filter{
			{Name: aws.String("tag:lazycloud:fleet"), Values: []string{acceptanceFleet}},
			{Name: aws.String("instance-state-name"), Values: []string{"pending", "running", "stopping", "stopped"}},
		}})
		if err != nil {
			t.Errorf("sweep: describe instances: %v", err)
			return nil
		}
		var ids []string
		for _, r := range out.Reservations {
			for _, i := range r.Instances {
				ids = append(ids, aws.ToString(i.InstanceId))
			}
		}
		return ids
	}
	if ids := live(); len(ids) > 0 {
		t.Logf("sweep: terminating %v", ids)
		if _, err := client.TerminateInstances(ctx, &ec2.TerminateInstancesInput{InstanceIds: ids}); err != nil {
			t.Errorf("sweep: terminate %v: %v", ids, err)
		}
	}
	// EC2 may list a terminated instance as running for a few seconds.
	for range 12 {
		if len(live()) == 0 {
			return
		}
		time.Sleep(5 * time.Second)
	}
	t.Errorf("acceptance instances still live after the sweep: %v", live())
}
