# Deployment infrastructure

- Scope infrastructure, IAM and secrets to the deployment namespace. Terraform
  must not require a live Kubernetes API.
- Own infrastructure identities in configuration; domain policy/rates/limits stay
  in Python and runtime values/secret bindings in Helm. Deploy roles need no state.
- Separate platform-managed secrets from operator-owned values; never put operator
  values in Terraform state. Bind explicit chart keys, not blanket secret imports.
- Preserve the generated AWS connection-policy owner and durable control role
  name; replacing that role breaks external trust.
- Scope Pod Identity and secret-reader access to their namespace. Shared managed
  Redis is not a per-replica resource.
- Runtime workspace buckets stay outside Terraform and require scoped cleanup.
  For database teardown, remove the branch before its object-owning role.
