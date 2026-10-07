# spike-fleet

Scratch packet on `prov-spike`, never merges. Real EC2 calls in `default`
(account 534742592531, `aws sts get-caller-identity`), us-east-2, on
2026-10-07 between 15:50 and 16:20 UTC. The harness is `spike/main.go`, its
own module, AWS SDK for Go v2 as the launcher uses it.

Setup: a scratch VPC 10.77.0.0/16 with an internet gateway and public subnets
in us-east-2a and us-east-2b, an egress-only security group, an SSM instance
profile, AL2023 `ami-0d3d85815a9746bc5`, 30 GiB encrypted gp3 root, IMDSv2,
the same user data everywhere. Pools: m7i.large, c7i.large, c6a.large in two
zones (six pools). Everything carried `lazycloud:task=spike-fleet` and is
gone (see Cleanup).

## Answers

| # | Question | Answer |
| --- | --- | --- |
| 1a | CreateFleet instant, Spot, hibernation configured | No. EC2 refuses it |
| 1b | Any Fleet type, Spot, that we can hibernate and start like a reserve | No. maintain launches it, but EC2 refuses every user stop |
| 1c | CreateFleet instant, on-demand, hibernation, then hibernate and start | Yes |
| 2 | Fleet instant latency against RunInstances | +0.5 s at the API, +0.2 s to running, +1.8 s to SSM online (medians) |
| 3 | Per-pool errors mappable to cooldowns | Yes, per type and subnet, with one trap (`UnfulfillableCapacity`) |
| 4 | Placement scores | Free, 20/s refill, per region or zone; useful for ranking regions, not a launch path |

The plan's premise "EC2 Fleet for launches" holds for Spot hosts that never
stop and for on-demand hosts. It does not hold for Spot reserves: they must
stay on RunInstances with a persistent request. That splits the launch path,
so it goes back to the user (see Recommendation).

## 1. Hibernation, stop and start, persistence

Instant Spot with hibernate or stop interruption behavior:

```
$ aws ec2 create-fleet --cli-input-json file://fleet-instant-spot.json   # Type instant, InstanceInterruptionBehavior hibernate (and stop)
InvalidParameterValue: SpotOptions.InstanceInterruptionBehavior must be null or "terminate" for given fleet type.
```

Instant Spot, default behavior, launch template with `HibernationOptions.Configured=true`.
The call succeeds but every pool fails:

```
"ErrorCode": "InvalidParameterCombination",
"ErrorMessage": "The request with type 'one-time' is not supported when HibernationOptions.Configured is set to 'true'."
```

Instant Spot without hibernation launches (c6a.large us-east-2b) on a
one-time request, and EC2 refuses a plain stop:

```
SpotInstanceRequest: {"Type": "one-time", "State": "active", "InstanceInterruptionBehavior": "terminate"}
$ aws ec2 stop-instances --instance-ids i-048db3cff91c7169e
UnsupportedOperation: You can't stop the Spot Instance 'i-048db3cff91c7169e' because it is associated with a
one-time Spot Instance request. You can only stop Spot Instances associated with persistent Spot Instance requests.
```

Type request refuses both behaviors:

```
InvalidParameterCombination: Parameter: InstanceInterruptionBehavior:hibernate and FleetType:request can not be specified together.
InvalidParameterCombination: Parameter: InstanceInterruptionBehavior:stop and FleetType:request can not be specified together.
```

Type maintain accepts hibernate and launches a hibernation-configured
instance on a persistent request (`Type persistent, InstanceInterruptionBehavior
hibernate`, untagged: the fleet does not pass its tags to the request). A user
stop is refused, before and after the fleet is deleted with
`--no-terminate-instances` (state `deleted_running`):

```
$ aws ec2 stop-instances --hibernate --instance-ids i-025ccfcab73b83e11
UnsupportedOperation: You can't stop the Spot Instance 'i-025ccfcab73b83e11' because it is in a fleet, which does not support stop.
```

Hibernate behavior on a maintain fleet therefore only covers EC2's own
interruptions. No Fleet type gives a Spot instance we can hibernate and start
again. Persistent RunInstances (today's reserve launch) remains the only way.

On-demand instant over the same six pools (`lowest-price`; on-demand has no
price-capacity-optimized) with hibernation configured works end to end:

```
instance i-0384731f92227ffb2 c6a.large us-east-2a, HibernationOptions.Configured true
15:57:00 stop --hibernate  -> UnsupportedOperation: not ready to hibernate yet   (our 2-minute floor holds)
15:58:19 stop --hibernate  -> stopping; StateReason Client.UserInitiatedHibernate
15:58:42 start-instances   -> pending; running at 15:58:59 (17 s)
after resume: uptime -s = 2026-10-07 15:56:02 (first boot); /run/spike-fleet (tmpfs) still present
```

So on-demand reserves can launch through an instant fleet and keep today's
stop and StartInstances path. The fleet tags the instance
`aws:ec2:fleet-id`; deleting the instant fleet leaves it alone.

## 2. Launch latency

Each round launches the modes in parallel; SSM online stands in for the
first heartbeat (the real agent install was not run). Times are seconds from
the call. RunInstances is today's reserve launch: persistent Spot, hibernate
behavior, hibernation configured, one pinned pool. Fleet modes ask for one
Spot instance over the six pools with price-capacity-optimized.

Set 2, five paired rounds, RunInstances pinned to m7i.large us-east-2a:

| mode | API return | running | SSM online |
| --- | --- | --- | --- |
| RunInstances | 1.37 (1.32 to 1.42) | 4.81 (4.49 to 5.34) | 13.23 (12.05 to 13.60) |
| CreateFleet instant | 1.90 (1.80 to 2.63) | 5.04 (4.88 to 5.78) | 15.02 (13.91 to 15.32) |

Set 1, five rounds, RunInstances pinned to c6a.large us-east-2a:

| mode | API return | running | SSM online | notes |
| --- | --- | --- | --- | --- |
| RunInstances | 1.57, 1.39 | 5.02, 4.61 | 14.12, 13.56 | 3 of 5 failed `InsufficientInstanceCapacity` |
| CreateFleet instant | 2.05 (1.84 to 2.80) | 5.52 (4.71 to 6.02) | 15.06 (14.65 to 15.86) | 5 of 5, moved to m7i.large 2a or c6a.large 2b |
| CreateLaunchTemplateVersion + instant | 0.46 + 1.92 = 2.37 (2.21 to 2.87) | 5.45 (5.13 to 6.05) | 14.54 (14.45 to 16.36) | |
| CreateFleet maintain | 1.4, instance id after 13.5 (13.2 to 135) | 16.2 | 26.3 (25.3 to 147) | async; worst 2 min 15 s |

Medians, ranges in brackets. In set 2 the fleet picked c6a.large every
time (4 in 2a, 1 in 2b) while RunInstances ran m7i.large, so part of the SSM
gap is the instance type. In the same pool and round (set 1 rounds 2 and 5,
both c6a.large 2a) instant was 0.5 s slower at the API and 1.0 s and 1.1 s
slower to SSM online.

What this means:

- Instant costs about half a second at the API. Running and boot are the
  same machine work.
- Instant took capacity 10 of 10 times. Pinned RunInstances failed 3 of 5 in
  set 1 on one pool, each after the SDK's three attempts (ICE is an HTTP 500,
  which the default retryer repeats).
- CreateFleet takes user data only from the launch template. Per-host user
  data (today's bootstrap carries the host id) costs a
  CreateLaunchTemplateVersion per launch, 0.3 to 0.5 s, and versions to
  delete (10,000 per template). Tagging the instance with the host id in the
  fleet's `TagSpecifications` and reading it from IMDS
  (`InstanceMetadataTags=enabled`) would avoid that. Not measured.
- maintain is not a launch path: 13 s to 135 s before the instance exists.

API buckets in us-east-2 (Service Quotas): RunInstances 5 burst, 2/s refill;
CreateFleet 50 burst, 5/s; CreateLaunchTemplateVersion 100 burst, 5/s;
StartInstances 5 burst, 2/s.

## 3. Errors per pool

CreateFleet instant returns HTTP 200 with `Errors[]`, one entry per pool
tried, carrying `Lifecycle`, `Overrides.InstanceType`, `Overrides.SubnetId`,
`ErrorCode` and `ErrorMessage`. No zone field: the zone comes from our
subnet map. Region is the client's. Shapes seen:

```
quota (X Spot quota 0, x2iedn.xlarge):
  [spot, x2iedn.xlarge, subnet-09d1..(2a), MaxSpotInstanceCountExceeded, "Max spot instance count exceeded"]
quota, on-demand (X on-demand quota 0):
  [on-demand, x2iedn.xlarge, subnet-03ee..(2b), VcpuLimitExceeded, "You have requested more vCPU capacity than your current vCPU limit of 0 ..."]
price (MaxPrice 0.001):
  [spot, c6a.large, subnet-09d1..(2a), SpotMaxPriceTooLow, "Your Spot request price of 0.001 is lower than the minimum required Spot request fulfillment price of 0.0218."]
capacity (real ICE, c6a.large 2a during set 1):
  [spot, c6a.large, subnet-09d1..(2a), InsufficientInstanceCapacity, "We currently do not have sufficient c6a.large capacity in the Availability Zone you requested (us-east-2a). ..."]
type not offered in the zone (trn1.2xlarge in 2a):
  [spot, trn1.2xlarge, subnet-09d1..(2a), InvalidFleetConfiguration, "Your requested instance type (trn1.2xlarge) is not supported in your requested Availability Zone (us-east-2a)."]
```

The trap: the fleet tries at most three pools. The rest come back as
`UnfulfillableCapacity` ("Unable to fulfill capacity due to your request
configuration"), whatever their state. Six pools all priced at 0.001 gave
three `SpotMaxPriceTooLow` and three `UnfulfillableCapacity`; the mixed
request gave quota, price and zone errors for three pools and
`UnfulfillableCapacity` for the fourth.

Mapping to `capacityRefusal` and `quotaRefusal` in `aws.go`:

| ErrorCode | today | cooldown key |
| --- | --- | --- |
| InsufficientInstanceCapacity | capacity | region, zone (subnet), type, market |
| MaxSpotInstanceCountExceeded, VcpuLimitExceeded | capacity + quota | region, market, family quota class |
| SpotMaxPriceTooLow | capacity | region, zone, type, Spot |
| InvalidFleetConfiguration | not mapped | region, zone, type; catalog error, long cooldown |
| InvalidParameterCombination | not mapped | none; a request bug, fail the launch |
| UnfulfillableCapacity | not mapped | none; the pool was not tried |

Codes are the RunInstances ones except `InvalidFleetConfiguration` and
`UnfulfillableCapacity`. A pool cools only on its own code; a launch with any
instance succeeds and ignores the errors of pools it skipped.

## 4. GetSpotPlacementScores

Free ("There is no additional charge", EC2 user guide, Spot placement score).
Rate: 100 burst, 20/s refill (quotas L-71F2A72B, L-E18E92FB). Target capacity
is capped by recent Spot usage: 512 vCPU worked, 1,000 vCPU and 2,000 vCPU
returned `TargetCapacityLimitExceeded`. AWS may also cap new request
configurations per 24 hours when use looks unusual, and wants at least three
instance types or it returns a low score. Calls took about 0.7 s through the
CLI.

```
4 types (m7i, c7i, c6a, m6a).2xlarge, 1 instance, per region:  us-east-1 9, us-east-2 9, us-west-1 9, us-west-2 9
same, single zone (10 rows returned): use1-az6 9, use1-az5 5, use1-az2 5, use2-az3 9, usw1-az1 9, usw1-az3 9, usw2-az1..az4 9
5 types .8xlarge, 10 instances:  us-east-2 2, us-east-1 9, us-west-1 9, us-west-2 9
3 types .8xlarge, 32 instances:  us-east-2 1
g5/g6/g4dn/g6e.xlarge, single zone: usw1-az1 9, usw2-az3 2, every other zone 1
g5/g6/g6e.48xlarge, 4 instances: us-east-1 1, us-east-2 1, us-west-2 1 (us-west-1 absent)
```

Price-capacity-optimized picks a pool inside one region's request. It cannot
choose a region, and it only applies to Spot. Placement scores answer the
region question: us-east-2 drops to 1 or 2 at ten 8xlarge hosts while the
other three regions stay at 9. So PCO alone is enough within a region; it is
not enough across regions. One score call per shape and region set per
planner pass, cached for minutes, fits the rate limit. Scores are a hint, not
a reservation.

## Recommendation for packet B

1. Spot hosts that never stop (serving, builds, one-time): CreateFleet
   instant, Spot, price-capacity-optimized, the offer's types and its
   region's zones as overrides, target 1. Cool pools from `Errors[]` by the
   table above.
2. Spot reserves: stay on RunInstances, persistent, hibernate behavior.
   B walks the offer's pools itself, best first (zone Spot price, placement
   score, cooldowns), so a reserve keeps today's stop, StartInstances and
   `endSpotRequest` path.
3. On-demand hosts and reserves: CreateFleet instant, on-demand, with
   hibernation configured for reserves; stop and start as today.
4. Region choice: placement scores per shape across the four regions, joined
   with full cost in `RankOffers`; PCO inside the chosen region.
5. User data: one launch template per region and node image, host id from an
   instance tag read through IMDS, so a launch is one call. Measure it in B.
6. Infra: CreateFleet needs the `AWSServiceRoleForEC2Fleet` service-linked
   role. EC2 created it on this spike's first call; the platform role needs
   `iam:CreateServiceLinkedRole` for it, or Terraform creates it.

Points 1 and 2 split the Spot launch path in two. The alternative is one path
on RunInstances for all Spot, with B ranking pools; it loses PCO's pool choice
(10 of 10 against 2 of 5 in set 1). I recommend the split; the user decides,
since the plan said Fleet for all launches.

## Unverified

- The real agent install and its first heartbeat (SSM online stood in).
- Pool choice under prod load and with GPU types; prod launched m7i.large in
  us-east-2a during the spike.
- Host id from instance tags via IMDS, and its latency.
- Whether a maintain fleet's hibernate behavior helps when EC2 interrupts.

## Cleanup

Removed: 2 launch templates (`spike-fleet-hib`, `spike-fleet-plain`, with
their per-launch versions), 31 fleets (all `deleted`), 30 instances
(terminated), 7 tagged Spot requests and the maintain fleet's untagged
`sir-zpizp1jk` (cancelled), volumes (0 left), security group, 3 subnets,
route table, internet gateway, VPC `vpc-034ae3ac3ba818787`, IAM role and
instance profile `spike-fleet-ssm`, and the service-linked role
`AWSServiceRoleForEC2Fleet` (deletion `SUCCEEDED`). Not touched: prod host
`i-077652e1809f18b78` and its request `sir-314zqr5k`. Spend: under $0.20.
