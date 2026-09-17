# Platform ownership cutover

Revision `0005_platform_namespace` moves platform capacity into an application
namespace. Tenant workspaces, users, billing, workloads, and customer cloud
connections retain their ownership. Platform unit IDs, names, generations,
provider resources, recovery records, and cleanup history remain intact.
AWS checkpoints retain their original resource namespace for names and tags,
including customer-owned pools. Database ownership can change without creating
a second fleet or losing the first fleet's cleanup identity.

This upgrade requires downtime. Stop old writers before migration. After the
ownership change, recover by moving forward with a corrected build; an older
build cannot read this schema.

1. Inventory the platform connection, its active authorization, node role and
   instance profile, regional networks, units, and provider resources. Complete
   pending authorization changes or cleanup before adoption.
2. Import the existing connection UUID into Terraform's `random_uuid.fleet_provider`.
   Set `fleet_node_identity_name` in the deployment's private variables to the
   existing node IAM name. Import the exact role, profile, and
   `managed-node-diagnostics` inline policy into `aws_iam_role.fleet_node`,
   `aws_iam_instance_profile.fleet_node`, and `aws_iam_role_policy.fleet_node_diagnostics`.
   Review the plan. Existing IAM identities and networks must not be replaced.
3. Publish the complete release artifacts. Pause Argo automatic sync and stop
   admission. Drain active work, or stop it when the deployment owner permits
   interruption. Stop every old API and scheduler replica.
   Keep PostgreSQL, Redis, and deployment workload identity available.
4. Apply the reviewed infrastructure configuration and publish descriptor version 8.
   The platform secret supplies `LAZYCLOUD_PLATFORM_CAPACITY_AWS__EXTERNAL_ID`.
   Preserve the existing external ID. Helm supplies the rest of the binding as
   `LAZYCLOUD_PLATFORM_CAPACITY_AWS`.
5. Select the new release and sync. The database job compares the configured AWS
   account, provider reference, roles, profile, external ID, and networks with
   the legacy connection before changing ownership. A mismatch aborts the transaction.
   Active work also blocks migration. The initializer then validates the live
   provider binding before application startup.
6. Replace old platform workers through their owning units. Migration revokes their
   enrollment, join, and worker credentials. New workers must enroll under the
   platform namespace with no user owner. Verify provider inventory, durable
   enrollment, scheduler intake, gateway connectivity, and current artifact identity.
7. Run a fresh client image build and function invocation. Check app and job
   scheduling, customer cloud isolation, and that account compute settings exclude
   platform machines. Repeat initialization and deployment sync without a human token.
8. Revoke the temporary deployment repair token by its exact ID and remove only
   its `LAZYCLOUD_TOKEN` property from the operator secret after confirming no
   deployment consumer remains. Preserve all unrelated credentials and resources.
   Restore Argo automatic sync and remove acceptance resources.

New installations run schema migration and `lazycloud-admin platform initialize`
before the API and scheduler. Create human administrator access separately with
`lazycloud-admin auth bootstrap`. Local Compose's optional `customer-compute`
profile enrolls customer-owned test machines and requires its own administrator
credential. The default platform startup has no human credential dependency.
