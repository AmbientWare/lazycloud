# Platform EKS

This module owns LazyCloud's AWS infrastructure: EKS, Redis, IAM, ECR, S3,
Secrets Manager containers, the PlanetScale branch, Argo CD, and the platform
account's connected-AWS fleet identity. Customer accounts remain owned by the
CloudFormation connection flow.

Pangolin is an external prerequisite. This module does not install or manage the
Pangolin server. It publishes the supplied endpoint, Integration API URL,
and organization ID to the chart.

## Pangolin prerequisites

Complete these Pangolin steps before the first deployment:

- an activated Enterprise license on the Pangolin server;
- an Integration API key scoped to the LazyCloud organization;
- a load-balancer route to Pangolin's Integration API listener (port 3003 by
  default), separate from the dashboard and tunnel API listeners;
- a verified domain covering the hostname in `gateway_public_http_url`.

`pangolin_api_url` includes `/v1`. Before deployment,
`<pangolin_api_url>/openapi.json` must return Pangolin's Integration API
document without authentication. A dashboard URL or tunnel API URL returns 404
and the bootstrap correctly refuses to create any platform identity.

For Pangolin 1.21.1, grant the key these actions:
`createSite`, `deleteSite`, `getSite`, `listSites`, `createClient`,
`deleteClient`, `getClient`, `listClients`, `createSiteResource`,
`deleteSiteResource`, `listSiteResources`, `listResourceUsers`, `setResourceUsers`,
`createResource`, `deleteResource`, `listResources`, `updateResource`,
`createTarget`, `getTarget`, `listTargets`, `updateTarget`,
`createOrgDomain`, `deleteOrgDomain`, `getDomain`, `listOrgDomains`, and
`getDNSRecords`.

The in-cluster bootstrap creates one Newt site per `pangolin_site_replicas`, one
machine client per `control_plane_replicas`, the public resource, and
health-checked targets. Both counts default to two. It stores their credentials
in the Terraform-created `<deployment>/pangolin-runtime` Secrets Manager entry.
A repeat deploy validates and reuses the same identities; increasing either
count creates only the missing ordinals.

Do not reduce either fixed platform replica count. The bootstrap refuses a
reduction rather than leave a stored connector or machine client with
privileged access. Workload nodes and customer agents still scale without
operator configuration.

Set these non-secret values in `terraform.tfvars`:

- `gateway_public_http_url`
- `github_redirect_uri`
- `pangolin_api_url`
- `pangolin_endpoint`
- `pangolin_organization_id`

Write the Pangolin API key to the operator Secrets Manager document.
`secrets.tf` owns the exact key list. Terraform never receives the value, and no
operator copies connector credentials.

The Pangolin installation must use the vendor-supported production topology for
the purchased Enterprise edition. Pangolin's
[clustering guidance](https://docs.pangolin.net/self-host/advanced/clustering)
requires vendor engagement, a shared PostgreSQL database and Valkey, redundant
Pangolin, DNS, Traefik, and Gerbil instances, and an operator-supplied HA load
balancer. This repository treats the resulting API and tunnel endpoint as an
external service boundary instead of maintaining a second implementation of
that cluster.

## Ownership rules

`provider_aws.connection_policy` owns the connected-account permission set.
Terraform consumes its rendered policy; do not maintain another list here.

`control_role_name` is a public contract after a customer connects. Customer
trust policies name its ARN, and recreating the same IAM name does not restore
the old role identity.

Every global AWS name carries `var.deployment` except that control role. The
deployment name also prefixes Pangolin sites and clients. Two deployments in
one account therefore need distinct `control_role_name` values, public
hostnames, deployment names, and Stripe test or live accounts.

See `LIFECYCLE.md` for creation and teardown.
