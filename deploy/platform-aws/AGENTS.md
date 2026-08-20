# Platform AWS

Terraform for everything this platform runs on. `README.md` is the operator
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
