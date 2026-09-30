# Deployment

- Keep canonical Compose, typed settings and health checks aligned with real
  dependencies. Operational instructions belong in the deployment README.
- Migrate before new processes run. Preserve writer compatibility or stop old
  owners before the cutover; never roll back to code incompatible with the schema.
- Validate the changed runtime boundary, not health checks alone. Recreate shared
  network namespaces with their sidecars and reload ingress after API replacement.
- Run workers through enrolled agents, with distinct identities/state and explicit
  networking. Keep service settings and every deployment consumer synchronized.
- Release one complete immutable manifest with platform/worker images, agent and
  AMIs. Promote the same artifacts; do not rebuild during deployment or app builds.
  Activate compatible releases through their durable owner.
- Terraform owns non-secret infrastructure descriptors, Helm runtime settings and
  secret bindings, CI builds/releases, and Argo installs. CI does not run Helm.
- Root Argo tracks main; deployment values track their deployment branch. Preserve
  operator-controlled pauses and update affected runbooks.
