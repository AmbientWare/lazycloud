# Platform core

Create this shared cluster once, then attach each deployment through
`deploy/platform-deployment`. This module owns the VPC, the EKS Auto
Mode cluster and its identities, the image repositories, the cluster's OIDC
provider, the EBS storage class, and Argo CD with its root Application. It is
applied once and rarely.

A deployment is `deploy/platform-deployment`, applied once per deployment with
its own state. It reads this module's outputs and attaches itself: Pod Identity
associations in its namespace, a secret-reader role the OIDC provider trusts, a
Redis the cluster's VPC may reach. Bring-up and teardown of the pair are in
`../platform-deployment/LIFECYCLE.md`.

## Applying

```sh
terraform -chdir=deploy/platform-core init \
  -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=platform-core/lazycloud.tfstate"
terraform -chdir=deploy/platform-core plan -out=core.tfplan
```

Review `core.tfplan`, then apply with
`terraform -chdir=deploy/platform-core apply core.tfplan`.

Use the shared [S3 state configuration](../terraform-state/README.md). An existing
installation transfers its current state with `init -migrate-state` before apply.

`terraform.tfvars` carries `cluster_api_cidrs`, which must include the address
this apply runs from: the Kubernetes and Helm providers reach the cluster's API
through it. When that address changes, `terraform apply
-target=aws_eks_cluster.control_plane` moves the allowlist through the AWS API
alone, and a full apply follows.

`TF_VAR_github_app_private_key` must be in the environment. Argo reads the
repository with the organisation's GitHub App, and Terraform writes that key
into Argo's repository-credentials Secret directly, because External Secrets is
one of the things Argo installs and cannot also be what Argo needs to start.

The Kubernetes and Helm providers run `aws eks get-token` with the ambient
operator identity. Install the AWS CLI before applying. Generating tokens when
needed keeps authentication valid during a cluster update or a long apply.

After a failed apply, inspect the provider's resources and compare their IDs
with state before retrying. An API error can leave a created cluster outside
Terraform state; destroying the state alone will not remove it.

## What Argo runs

The root Application reads `deploy/argocd/apps` on `main`. A file there is a
thing this cluster runs: the External Secrets operator, installed once into
`external-secrets`, and one Application per deployment, `lazycloud-prod.yaml`
reading the `prod` branch into namespace `lazycloud-prod`. A change to a file
there takes effect on the next sync with no deploy in between. Adding a
deployment is adding its file, once its first deploy has created its branch.

## Node capacity

`node_capacity.tf` owns the `platform` NodeClass and `platform-spot` NodePool.
The Argo bootstrap release installs them as infrastructure before it waits for
Argo pods, so initial node provisioning does not depend on Argo reconciliation.
They reuse the cluster's node role, access entry, security group and subnets.
Auto Mode chooses instance sizes and counts from pod requests. The pool permits
amd64 C/M/R Spot instances and serial voluntary disruption, with five minutes
before consolidation to avoid churn during short deployment bursts.

Migrate an existing built-in pool in stages. Install and validate custom
capacity first, deploy the replica placement rules through Argo, then drain
selected old nodes one at a time. Disable the built-in pool only after custom
capacity is serving and replacement provisioning is proven. Preserve node
access, the cluster and all data. [Migration acceptance](../spot-capacity.md)

When adopting an existing built-in node role, import its access entry and
`AmazonEKSAutoNodePolicy` association into `aws_eks_access_entry.node` and
`aws_eks_access_policy_association.node` before applying. EKS requires
`compute_config.node_role_arn` to be absent when built-in pools are disabled.
The NodeClass still uses the same role; Terraform owns its explicit node access.

## Images

One repository per control-plane image, `lazycloud/<name>`, tagged by the
commit that built it and immutable. Ship publishes images for a commit and records their executable digests in
the release. Deploy selects that release; promotion reuses it without rebuilding.

## Taking it down

Only after an explicitly authorized teardown, retire every deployment using
[the lifecycle guide](../platform-deployment/LIFECYCLE.md). Review a destroy
plan against the exact remaining shared resources before running:

```sh
terraform -chdir=deploy/platform-core destroy
```

The destroy leaves detached EBS volumes behind, one per dynamic claim any
deployment provisioned. They bill until removed:

```sh
aws ec2 describe-volumes --filters Name=status,Values=available \
  --query 'Volumes[].[VolumeId,Size,Tags[?Key==`Name`]|[0].Value]' --output text
```
