# Deployment

Root `compose.yaml` is the canonical local stack; Compose and Helm remain
aligned with Pydantic settings and provide PostgreSQL, Redis, object storage,
health checks, and explicit service ownership. Never commit secrets, generated
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
- Raw `docker compose up` leaves the control plane and scheduler without AWS
  credentials, and connection validation can never succeed. The canonical
  connected activation path is `uv run python deploy/compose/activation.py`
  from the repository root: it validates the scoped configuration directory
  (default `~/.lazycloud/compose-aws`, or `LAZYCLOUD_COMPOSE_AWS_CONFIG_DIR`),
  asserts the non-root control role through STS, verifies the control stack,
  then applies `deploy/compose/aws-profile.yaml` and recreates only the
  credential-consuming services before waiting for their health
  (`--skip-compose` validates without touching Compose). The scoped directory
  contains only the test source credentials and the role-chain profiles ending
  in `compose-control` (never the root `default` keys); the services refresh
  through the SDK credential chain, so no fixed session expiry exists and the
  command never creates or copies credential files.
- The acceptance host may run AWS CLI v1: never pass v2-only flags such as
  `--no-cli-pager`; set `AWS_PAGER=""` in the subprocess environment instead.
