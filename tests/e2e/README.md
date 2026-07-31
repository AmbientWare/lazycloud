# Scoped End-to-End Acceptance

This directory contains opt-in production scenarios. It is not part of ordinary
`pytest`.

Each scenario proves one user-visible capability through the public SDK, CLI,
API, or browser path against an already-prepared environment. It may create only
uniquely named resources owned by that scenario, must clean them through their
public owner, and returns nonzero when behavior or cleanup fails. Missing live
authorization or prerequisites return exit code `77`.

## Boundaries

- `local/` targets an already-healthy root Compose stack.
- `external/` targets an explicitly authorized provider, cluster, Tailnet, or
  GPU environment.
- Release publication, deployment, migration, connection administration,
  credential creation, and broad infrastructure teardown belong to deployment
  or operator workflows outside this directory.
- Shared helpers stay limited to secret-safe command, live-gate, polling, and
  exact cleanup primitives.

Run Python scenarios as modules from the repository root:

```sh
uv run python -m tests.e2e.local.function.scenario_invoke --live
```

`local/` scenarios that build an image need a Linux host and two things the
stack does not arrange on its own:

- Build the worker image explicitly. `container-worker` sits behind the
  `owner-direct-worker` profile, so `docker compose up --build` never builds it,
  and the only symptom is the agent restart-looping on "Unable to find image":

  ```sh
  docker compose --profile owner-direct-worker build container-worker
  ```

- Give the agent state directory (`LAZYCLOUD_COMPOSE_AGENT_STATE_DIR`) at least
  21 GiB free — an image build reserves `per_build_max_bytes` plus
  `minimum_free_bytes` before it starts. The agent passes that path to the
  Docker daemon as the bind source for worker slots, so it must resolve
  identically for the agent process and the daemon: keep it on daemon-local
  storage rather than a remote or virtualised mount.

The remaining local, Tailnet, GPU, and browser scenarios document their own
additional prerequisites in their modules.

## Connected AWS

Connected AWS is split into three independent paid scenarios. The ambient AWS
CLI identity is the customer identity; the only product target supplied to
connection and cleanup is its 12-digit account ID.

For a local platform, expose a refreshable platform profile only to the control
plane and scheduler:

```sh
LAZYCLOUD_COMPOSE_AWS_CONFIG_DIR=/absolute/path/to/.aws \
LAZYCLOUD_COMPOSE_AWS_PROFILE=compose-control \
docker compose up -d
```

The agent and its workers do not receive this profile. Production deployments
use workload identity through the same AWS SDK credential chain.

Create or resume the public account connection and perform only the
CloudFormation customer authorization returned by LazyCloud:

The template URL and platform principal are the immutable release values the
control plane advertises; passing them explicitly is what proves the customer
action was not substituted. Both are read from the deployment environment:

```sh
uv run python -m tests.e2e.external.aws.account_connection \
  --account-id <12-digit-account-id> \
  --template-url "$LAZYCLOUD_AWS_CONNECTION_TEMPLATE_URL" \
  --platform-principal-arn "$LAZYCLOUD_AWS_CONNECTION_CONTROL_PRINCIPAL_ARN" \
  --execution-role-arn "$LAZYCLOUD_E2E_AWS_CUSTOMER_STACK_EXECUTION_ROLE_ARN"
```

Select AWS and prove the same one-ready-machine baseline that the platform keeps
for fast starts:

```sh
uv run python -m tests.e2e.external.aws.one_machine_readiness
```

Run one paid Function on that warm baseline to prove the complete data plane. It
must return the exact result, retain the log marker in public task logs, and run
on the exact warm-baseline machine:

```sh
uv run python -m tests.e2e.external.aws.connected_aws_function \
  --run-id <id> --app-slug <slug>
```

Finally, disable the warm floor, disconnect through the public owner, wait for
public zero capacity and cost, and perform one workspace-scoped AWS
corroboration:

```sh
uv run python -m tests.e2e.external.aws.cleanup \
  --account-id <12-digit-account-id>
```

The cleanup scenario fails closed if AWS reports capacity for the workspace or
if the public connection requires a customer action that the product did not
make automatable. It never inventories or changes unrelated Tailnet or AWS
resources.
