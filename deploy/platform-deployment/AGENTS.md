# Platform deployment

Terraform for one deployment of the platform on the cluster
`deploy/platform-core` declares: the managed Redis it coordinates through, the
buckets, secret documents, database branch and fleet network around it, and
the identities its workloads hold in its namespace. Applied once per
deployment, `lazycloud-prod` and later `lazycloud-staging`, each with its own
state. `README.md` is the operator runbook.

- The boundary is ownership, not provider. Anything in an account we hold
  credentials for is declared here. CloudFormation is only for a customer's own
  account, where we cannot run Terraform at all.
- `var.deployment` is the namespace. Every globally-named resource and secret
  path carries it, the PlanetScale database is named by it, and the Pod
  Identity associations name it, so the one word that tells two deployments
  apart in AWS is the one that tells them apart in the cluster.
- Nothing here reaches the Kubernetes API. Every association is an AWS call
  keyed on the cluster's name, so an apply needs no kubeconfig and no listed
  address, and a deployment can be applied from anywhere. Keep it that way:
  the day this module declares a Kubernetes object it inherits the core's
  allowlist and the core's providers.
- Production is persistent. Review every resource replacement against the live
  installation. Resolve provider/state discrepancies before applying.
- Export resource identities through configuration.tf. Application policy, prices,
  limits and secret property bindings belong to Helm, not Terraform variables.
  The deploy role reads only this descriptor and has no state-bucket access.
- A deployment's credentials are two JSON documents, split by who can produce
  the value. `<deployment>/platform` is written here when its inputs change; `<deployment>/operator` is declared here and written by a person.
  Secrets Manager bills per entry, and one entry cannot hold both: a document is
  written atomically, so a single one would have this configuration dropping
  every field an operator added. `ignore_changes` does not rescue it, because
  the database URL carries a password that rotates.
- Never put a value only a person can obtain into the platform document. That is
  the line the split exists to hold, and it is what keeps operator credentials
  out of the state file.
- Name every variable in the chart rather than extracting a document wholesale.
  `dataFrom` copies whatever the document happens to contain, so a variable
  nobody wrote is first reported by a pod that will not start.
- Never hand-write the connection role's permissions. They are generated from
  `provider_aws.connection_policy` into `connection-role-policy.json`, and CI
  fails on a stale file. A second copy of a permission set drifts into a launch
  denial that names an API call rather than the policy behind it.
- `control_role_name` is a durable external contract once a customer connects.
  A customer's trust policy names it, and AWS resolves that name to a unique ID
  that recreating cannot restore. Unset, it is `<deployment>-control-principal`.
- A workload's AWS identity is its own, and never the node's. Workload pods hold
  theirs through Pod Identity: an association names this cluster, this
  namespace and one service account, so `lazycloud-staging/control-plane`
  cannot hold prod's role. The secret reader is the one IRSA identity, because
  the External Secrets store is reconciled by an operator shared across
  namespaces and the only identity it can present per store is a service
  account token; its trust names this namespace's subject and nothing else.
- Redis is managed and outside the cluster. It holds the leases the scheduler
  serialises capacity work on, so an in-cluster Redis per replica is two
  schedulers that cannot see each other. It runs a primary and a standby with
  automatic failover: what it holds is rebuilt on reconnect, so the standby
  protects nothing durable, only the minutes a lost node would otherwise cost.
- Buckets a workspace creates at runtime are not declared here and do not go away
  with `terraform destroy`. The control plane names them from
  `workspace_bucket_prefix` and creates them lazily, so a teardown that only runs
  Terraform leaves one per workspace behind.
- The PlanetScale branch role cannot be dropped while it owns objects, and it
  owns every table the schema created. Destroying the branch takes the role with
  it; destroying the role first fails with `Role is still referenced`.
