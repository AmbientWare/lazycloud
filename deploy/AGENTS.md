# Deployment

Root `compose.yaml` is the canonical local stack; it stays aligned with
Pydantic settings and provides PostgreSQL, Redis, object storage, health
checks, and explicit service ownership. Never commit secrets, generated
credentials, or local state. While predeployment, maintain one fresh-install
database baseline and no historical transition paths. Validate the affected
render/build/startup boundary and update operator docs when behavior changes.

For live test deployment, the Tailnet explicitly selected or supplied by the
user is an approved target regardless of its account name. Read its existing
state before changing it and mutate only exact LazyCloud-owned test resources.
Preserve unrelated users, devices, tags, grants, ACL rules, DNS and route
configuration, OAuth clients, and keys. Do not apply a whole-policy owner,
rotate shared credentials, or perform broad teardown against a provided
Tailnet; cleanup must target only resources proven to have been created for the
current test.

## Connected-AWS acceptance environment

- `default-test` is the only AWS profile for acceptance work: a role profile
  chaining through `default-test-source` into the
  `lazycloud-default-test-operator` role. The `default` profile is root
  bootstrap authority — never use it for tests, Compose, or stack automation;
  its only accepted use is one-time provisioning explicitly directed by the
  owner.
- The operator role deliberately cannot create or delete CloudFormation stacks
  directly. Customer `compute-connection-*-g*` stacks require the execution
  role recorded in the protected `.env` as
  `LAZYCLOUD_E2E_AWS_CUSTOMER_STACK_EXECUTION_ROLE_ARN` (also the
  `CustomerStackExecutionRoleArn` output of the `lazycloud-default-test-operator`
  stack). `deploy/connected-aws/customer_stack.py` accepts it as
  `--execution-role-arn`.
- `docker compose up` is the only activation path. The control plane and
  scheduler mount `LAZYCLOUD_COMPOSE_AWS_CONFIG_DIR` (default
  `~/.lazycloud/compose-aws`) at `/run/lazycloud/aws` and read the role chain
  from it, so a connected stack differs from a local one by that variable alone.
  The directory holds only the test source credentials and the role-chain
  profiles ending in `compose-control`, never the root `default` keys; the SDK
  refreshes the chain, so no fixed session expiry exists. A stack whose
  credentials do not resolve refuses to start rather than reporting healthy and
  failing every connection later.
- `tailnet-gateway` runs in the control plane's network namespace, so recreating
  `control-plane` destroys it and Compose does not bring it back. The stack then
  reports every service healthy while the control plane is absent from the
  tailnet, and remote nodes fail to resolve it as a peer minutes later. Follow
  any `control-plane` recreate with `docker compose up -d tailnet-gateway`, and
  confirm `tailscale status` reports `Online: True` before trusting a run.
- Read the control plane's tailnet name from the running sidecar rather than
  assuming it. A device that lost its name to a collision keeps the `-1` suffix,
  and `LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL` must match what the sidecar actually
  holds. Do not delete a tailnet device to reclaim a nicer name: it invalidates
  the sidecar's identity and takes the control plane off the tailnet.
- The acceptance host may run AWS CLI v1: never pass v2-only flags such as
  `--no-cli-pager`; set `AWS_PAGER=""` in the subprocess environment instead.
