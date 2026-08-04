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

## Publishing a release

Do not perform the sequence by hand. It was written down here first and was
still performed wrong — two images rebuilt out of nine, and the mismatch
surfaced an hour later on a running EC2 node.

```bash
uv run python deploy/release.py \
  --bucket "$AWS_RELEASE_ASSET_BUCKET" \
  --worker-repository <registry>/lazycloud-container-worker
```

It refuses a dirty tree, because a version label that names a revision the
artifacts do not contain is worse than no label. It builds every image and the
agent executable from that one revision, publishes the worker image and the
release, writes the manifest's own values into `.env` rather than a
transcription of them, and puts the sidecars back afterwards.

`--arch` defaults to `amd64`, which is what AWS node classes consume. Building
`arm64` needs binfmt registered first (`docker run --privileged tonistiigi/binfmt
--install arm64`), or the cross-architecture stage fails with `exec format
error`.

### Did it reach the nodes

Publishing replaces no running instance. An Auto Scaling group whose launch
template moves v1→v2 leaves every InService node on v1 by design, so the last
thing the command does is say which release each node is actually running, and
exit non-zero when that is not this one.

First it compares the two processes that each compose a pool's bootstrap —
`control-plane` and `scheduler` — over the variables the release publishes plus
`LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL`, read from the environment each container
actually holds. While those disagree the launch template alternates on every
reconcile and no node settles on either version, so nothing said about nodes
afterwards would mean anything. Both holding the *previous* release fails here
too: it is the shape a restart that did not take leaves behind.

Then, per pool, it prints every node's booted launch-template version against
the pool's current one and repeats the reading until the record has been
rewritten by a reconcile newer than the restart — up to three minutes, since
the pooled reconcile runs on a 60s timer. Rows print every cycle whether or not
anything is stale; a report that prints only problems reads the same as one that
failed to look.

It only reads, and only the platform's own durable record: the control plane
owns the writes that would replace a node (see **Draining capacity**), and
re-querying EC2 would test EC2 rather than the release. The pool's current
launch-template version is the one value with no HTTP surface — it lives in the
pool's provider state — so both halves of the comparison come from one query at
one instant. A version the record cannot supply fails the report rather than
passing it.

`--skip-restart` skips the guard and the report along with the restart. The
release is published but this stack has not loaded it, so every node would read
as stale when nothing is wrong, and the closing line says published rather than
live.

The API answers on host port **8000** (container port 9000). `docker compose port
control-plane 9000` prints the mapping if it changes.

### The sidecar hazard

`control-plane` shares its network namespace with `tailnet-gateway` and
`public-ingress`. Recreating the control plane stops both, and Compose does
**not** bring them back:

```bash
docker compose up -d --force-recreate control-plane
docker compose up -d tailnet-gateway public-ingress   # required, every time
```

Skip the second command and the control plane is silently off the tailnet and
off the public origin: nodes join, report nothing, and are reclaimed at their
bootstrap deadline.

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

## Public ingress

`https://lazycloud.dev` reaches the origin through the `public-ingress`
connector. Routes are in `deploy/public-ingress/cloudflared.yml`, not the
dashboard; `deploy/public-ingress/README.md` owns mint and rotation.

Separate connector health from edge routing before anything else — the two
fail identically from outside:

```bash
docker compose exec -T control-plane python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:20241/ready',timeout=5).read().decode())"
```

`readyConnections` above zero means the connector is fine and the problem is at
the edge or in DNS. A `530`/`1033` with a healthy connector is DNS: the
hostname's record is not a proxied CNAME to this tunnel. Read the zone through
the API — `dig` cannot distinguish a flattened apex CNAME from an unrelated
proxied A record.

Repeated `control stream encountered a failure while serving` with every
network precheck passing means the tunnel no longer exists at Cloudflare, not a
connectivity fault.

## Secrets and rotation

| Secret | Where it lives | Rotate by |
| --- | --- | --- |
| Tailscale OAuth client | `.env`, `LAZYCLOUD_TAILNET_OAUTH_CLIENT_*` | Mint a new client owning the agent tag in the Tailscale admin console, update `.env`, recreate `control-plane` **and** `tailnet-gateway` |
| Admin scrape token | `.env` | Reissue through the CLI; `/metrics` is admin-gated and must stay so |
| Cloudflare tunnel credentials | file named by `LAZYCLOUD_PUBLIC_INGRESS_CREDENTIALS_FILE` | Mint a second tunnel, repoint both DNS records, recreate `public-ingress`, then delete the old tunnel — see `deploy/public-ingress/README.md` |
| Cloudflare API token | `.env`, `CLOUDFLARE_API_TOKEN` | Reissue in the Cloudflare dashboard; scoped to Tunnel:Edit, DNS:Edit, Zone:Read |

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

- **Prometheus, Alertmanager, alert meanings** — alerting is deferred
  (`plan/final.md`, decision 7). Metrics are exposed at the admin-gated
  `/metrics` and are correctly typed for a scraper; nothing scrapes them yet.
