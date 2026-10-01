# Deployment infrastructure

- Scope infrastructure, IAM and secrets to the deployment namespace. Terraform
  must not require a live Kubernetes API.
- Terraform owns infrastructure identities and the chart's infrastructure
  values (configuration.tf); product settings live in the chart's environment
  file and secret bindings in the chart. Deploy roles need no state.
- Separate platform-managed secrets from operator-owned values; never put operator
  values in Terraform state. Bind explicit chart keys, not blanket secret imports.
- The control plane role is the principal customer connection roles trust;
  replacing it breaks external trust.
- Scope Pod Identity and secret-reader access to their namespace.
- reference.tf holds what only main's deployment uses. Delete it whole at
  decommission; add nothing new to it.
- Runtime workspace buckets stay outside Terraform and require scoped cleanup.
  For database teardown, remove the branch before its object-owning role.
