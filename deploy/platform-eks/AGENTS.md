# Platform EKS

Terraform for everything this platform runs on: the cluster the control plane
runs in, the managed Redis it coordinates through, and the storage, secrets,
registry, database and fleet network around them. `README.md` is the operator
runbook and states the two deployment models.

- The boundary is ownership, not provider. Anything in an account we hold
  credentials for is declared here. CloudFormation is only for a customer's own
  account, where we cannot run Terraform at all.
- This is greenfield and stays greenfield. An apply produces the deployment; it
  does not adopt one. Do not add import blocks or reconciliation against
  hand-built resources.
- Never declare a secret's value. Declare the container and the access to it. The
  fleet external ID is the one exception and carries its reason in `fleet.tf`.
- Never hand-write the connection role's permissions. They are generated from
  `provider_aws.connection_policy` into `connection-role-policy.json`, and CI
  fails on a stale file. A second copy of a permission set drifts into a launch
  denial that names an API call rather than the policy behind it.
- `control_role_name` is a durable external contract. A customer's trust policy
  names it, and `prevent_destroy` exists because AWS resolves that name to a
  unique ID that recreating cannot restore.

- Auto Mode provisions everything, including the control plane. No node group is
  declared here and no workload names a node: Karpenter sizes from what the pods
  request, so a hand-declared pool would be choosing hardware on its behalf and
  paying for it whether or not anything lands there.
  The control plane does need `NET_ADMIN`, `NET_RAW` and a real `/dev/net/tun`
  for the tailnet device it holds. Those are properties of a pod and of the node
  image every node already runs, not reasons to pick an instance type.
- A workload's AWS identity is its own, assumed through the cluster's OIDC
  provider, and never the node's. The subject names service accounts exactly: a
  wildcard would let any pod in the namespace hold the role that reaches every
  workspace bucket and that the connected-AWS control role trusts.
- Redis is managed and outside the cluster. It holds the leases the scheduler
  serialises capacity work on, so an in-cluster Redis per replica is two
  schedulers that cannot see each other. It is single-node on purpose: what it
  holds is rebuilt on reconnect.
- Buckets a workspace creates at runtime are not declared here and do not go away
  with `terraform destroy`. The control plane names them from
  `workspace_bucket_prefix` and creates them lazily, so a teardown that only runs
  Terraform leaves one per workspace behind.
- The PlanetScale branch role cannot be dropped while it owns objects, and it
  owns every table the schema created. Destroying the branch takes the role with
  it; destroying the role first fails with `Role is still referenced`.
