# Operator Runbook

What you cannot derive from the code. Every command here was run against a live
stack on 2026-07-31; where a section covers work that has not landed, it says so
rather than guessing.

## Bring-up

```bash
cd /path/to/lazycloud
COMPOSE_PROFILES='*' docker compose build      # all source-bearing images together
docker compose up -d
until [ "$(docker compose ps --format '{{.Health}}' control-plane | head -1)" = healthy ]; do sleep 5; done
```

Build **every** source-bearing image in one command. Building a subset produces
a package-digest mismatch that the managed runtime rejects at container start,
and the error names the digest rather than the stale image.

The API answers on host port **8000** (container port 9000). `docker compose port
control-plane 9000` prints the mapping if it changes.

### The sidecar hazard

`control-plane` shares its network namespace with `tailnet-gateway`. Recreating
the control plane stops that sidecar, and Compose does **not** bring it back:

```bash
docker compose up -d --force-recreate control-plane
docker compose up -d tailnet-gateway          # required, every time
```

Skip the second command and the control plane is silently off the tailnet: nodes
join, report nothing, and are reclaimed at their bootstrap deadline. When the
public ingress lands (INFRA-06) it shares the same namespace and joins this rule.

A restarted gateway can also hold a stale netmap that lists deleted devices as
online and omits new ones. If a node is on the tailnet but unreachable from the
control plane, restart `tailnet-gateway` before investigating further.

## Reading a failed node

In this order. Stop at the first step that answers the question.

**1. The durable reason.** Always start here; it named every failure this
platform has had.

```bash
docker compose exec -T postgres psql -U lazycloud -d lazycloud -x -c "
select instance_id, status,
       payload->>'bootstrap_phase'          as phase,
       payload->>'bootstrap_failure_reason' as reason,
       payload->>'bootstrap_failure_detail' as detail
from compute_provider_instances
order by created_at desc limit 5;"
```

`reason` is a closed enum; `detail` is the excerpt the node sent with it. Treat
`detail` as sensitive — a bootstrap log can carry a credential.

**2. The durable event**, which carries the same excerpt and survives the
instance row:

```bash
docker compose exec -T postgres psql -U lazycloud -d lazycloud -x -c "
select created_at, action, level, resource_id, message
from events
where resource_type = 'provider-instance'
order by created_at desc limit 10;"
```

**3. Console output.** The only diagnostic that survives a node which never
reached userland — and it works on terminated instances.

```bash
CREDS=$(AWS_PROFILE=default-test-source aws sts assume-role \
  --role-arn arn:aws:iam::<account>:role/lazycloud-default-test-diagnostics \
  --role-session-name diag --query Credentials --output json)
export AWS_ACCESS_KEY_ID=$(echo "$CREDS" | jq -r .AccessKeyId)
export AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | jq -r .SecretAccessKey)
export AWS_SESSION_TOKEN=$(echo "$CREDS" | jq -r .SessionToken)

aws ec2 get-console-output --region us-east-1 --instance-id <i-...> --output text
```

**4. A shell on the node**, via SSM. Requires the node to be running and its
agent healthy enough to have registered with SSM.

```bash
aws ssm describe-instance-information --region us-east-1 \
  --query 'InstanceInformationList[].[InstanceId,PingStatus]' --output text

aws ssm send-command --region us-east-1 \
  --document-name AWS-RunShellScript \
  --targets Key=InstanceIds,Values=<i-...> \
  --parameters commands='journalctl -u lazycloud-agent -n 200 --no-pager'
```

The diagnostics role is deliberately separate from the acceptance operator role:
`ssm:SendCommand` is remote code execution on a running node, scoped by the
`cloud-pool:managed-by=control-plane` launch tag. Assume it to diagnose, not to
run acceptance.

## Draining capacity

A node whose bootstrap failed keeps running and holds the pool's slot. The
reclaim path handles the deadline case; to clear a pool by hand, drive the
policy to zero rather than terminating instances directly — the control plane
owns those writes and will otherwise relaunch.

```bash
uv run python - <<'PY'
from lazycloud.cli.control import compute_client
from shared.http.compute_policy import AwsWorkspaceComputePolicyPatch, WorkspaceComputePolicyPatchRequest
c = compute_client(timeout_seconds=30)
cur = c.policy()
c.patch_policy(WorkspaceComputePolicyPatchRequest(
    expected_revision=cur.revision,
    aws=AwsWorkspaceComputePolicyPatch(
        initial_cpu_workers=0, min_cpu_workers=0, max_cpu_instances=0
    ),
))
PY
```

Expect a `409` while a reconcile is in flight; retry. Watch it drain with
`uv run lazycloud compute instances`.

## Secrets and rotation

| Secret | Where it lives | Rotate by |
| --- | --- | --- |
| Tailscale OAuth client | `.env`, `LAZYCLOUD_TAILNET_OAUTH_CLIENT_*` | Mint a new client owning the agent tag in the Tailscale admin console, update `.env`, recreate `control-plane` **and** `tailnet-gateway` |
| Admin scrape token | `.env` | Reissue through the CLI; `/metrics` is admin-gated and must stay so |
| Cloudflare tunnel credentials | pending INFRA-06 | — |

Legacy credentials from the superseded architecture live outside the repo at
`~/.lazycloud-legacy-secrets/secrets-backup/`. They are **not** rotated. Anything
still live there (AWS keys, Cloudflare, WorkOS, Polar, Resend, Depot, Upstash,
Prefect) should be rotated and the directory deleted.

## Irreversible actions

Confirm the target belongs to the task before each of these. None can be undone.

- **Deleting a tailnet device.** The control plane's own device name is sticky to
  the device record: delete it and the sidecar comes back under a `-1` suffix,
  and every configured origin naming the old name breaks. See `deploy/AGENTS.md`.
- **Deleting a customer connection stack.** Removes the roles the control plane
  assumes; the connection must be re-established from scratch.
- **Deleting launch-template versions.** The pool cannot roll back to a template
  version that no longer exists.
- **`ssm:SendCommand`.** Arbitrary code on a running customer node.

## Not yet covered

- **Public ingress** — designed in `plan/14-ingress-design.md`, not deployed.
  Until it exists there is no public origin and managed pools are configured
  against the tailnet origin.
- **Prometheus, Alertmanager, alert meanings** — alerting is deferred
  (`plan/final.md`, decision 7). Metrics are exposed at the admin-gated
  `/metrics` and are correctly typed for a scraper; nothing scrapes them yet.
