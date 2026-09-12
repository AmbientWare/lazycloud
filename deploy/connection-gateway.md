# Connection gateway deployment

Agents connect to `tunnels.<public API hostname>:443`. The public NLB forwards TCP
to gateway port 8443 and preserves TLS end to end. The gateway verifies client
certificates and reaches the API at `control-plane:9000`. API and gateway pods have
separate Deployments. Two gateway replicas share one Service and load balancer.

The gateway generates its private key locally and obtains a one-hour certificate
through the public API. Renewal starts 20 minutes before expiry. Gateway pods hold
their own leaf/key, public trust, database credential, Redis URL, and the dedicated
bootstrap credential. They never mount the issuer key or an AWS credential.

Readiness requires a listening process, a valid leaf, PostgreSQL, and Redis. The
probe checks the age of `/tmp/lazycloud-connection-gateway.ready`. SIGTERM removes
readiness, asks agents to reconnect, and allows accepted streams 60 seconds. Pod
termination permits 90 seconds. NLB deregistration lasts 75 seconds and does not
force-close existing connections. API releases do not change the gateway image
when its installed source and dependencies are unchanged.

## Bootstrap credentials once

The existing operator Secrets Manager document owns three additional properties:

- `LAZYCLOUD_TUNNEL_ISSUER_CERTIFICATE_PEM`
- `LAZYCLOUD_TUNNEL_ISSUER_PRIVATE_KEY_PEM`
- `LAZYCLOUD_TUNNEL_GATEWAY_BOOTSTRAP_SECRET`

Initialize them after the operator document exists, before deploying the API:

```sh
uv run --frozen --group workspace python -m deploy.tunnel_identity aws \
  --secret-id lazycloud-prod/operator --region us-east-1 \
  --hostname tunnels.lazycloud.dev
```

This operator command uses AWS profile `default`. It preserves existing properties
and credentials, rejects an incomplete issuer, and promotes its new secret version
only if the version it read is still current. AWS rejects a promotion whose
[current version changed](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_UpdateSecretVersionStage.html).
No credential is printed. The command uses
`GetSecretValue`, `PutSecretValue`, and `UpdateSecretVersionStage` on that document.
It is not a deployment Job and does not run on redeploy.

External Secrets projects the CA only into the API at
`/run/lazycloud/tunnel-issuer/certificate.pem` and `private-key.pem`, with mode 0600.
The API receives their paths through `LAZYCLOUD_TUNNEL_ISSUER_CERTIFICATE_FILE` and
`LAZYCLOUD_TUNNEL_ISSUER_PRIVATE_KEY_FILE`. The dedicated bootstrap secret is an
environment variable in the API and gateway. Keep the issuer backed up in the
secret system. Its one-year validity needs a planned trust migration before expiry;
rerunning bootstrap never rotates it. Rotate the bootstrap credential by refreshing
External Secrets, then rolling the API and gateways through their secret revisions.

## Clean cutover

The approved cutover permits a brief interruption. Do not run old and new agent
protocols against mixed API replicas.

1. Inventory current agents, workloads, gateway resources, secret versions, DNS,
   registry images, and Terraform resource addresses. Preserve the current release
   and data for rollback. Finish local acceptance, including API replacement,
   gateway drain/loss, renewal, and worker/container traffic through the tunnel.
2. Create only the new `aws_ecr_repository.image["connection-gateway"]` and its
   lifecycle policy in `deploy/platform-core`. Bootstrap the operator CA. Publish
   infrastructure descriptor version 7. This preparation must not delete resources
   from the running installation.
3. Publish the complete release. Pause automated Argo reconciliation, stop
   admission, and retire the enumerated old agents. Stop the old API StatefulSet
   before starting the new Deployment so the shared Service cannot select mixed
   protocols. Sync migrations and new workloads without pruning old resources.
   The new API has no networking sidecar.
4. Read `connection-gateway` Service's assigned NLB hostname. Set that exact value
   as `connection_gateway_endpoint` in the existing `deploy/cloudflare` owner.
   Review and apply its single new `tunnels.<public hostname>` DNS-only CNAME.
   Preserve apex, wildcard, mail, and customer records. Never proxy this record
   through a TLS-terminating HTTP service.
5. Verify TLS hostname/trust through port 443, enroll a current agent, and run a
   function plus a streamed workload through it. Prove API replacement preserves
   its session, then drain each gateway under traffic. Resume admission only after
   the new route works.
6. Retire only the inventoried obsolete resources after the rollback decision.
   Prune those Kubernetes objects, apply the final Terraform removal plan, and
   verify no unrelated resource changed. Restore automated Argo reconciliation.

The retirement inventory includes Kubernetes `tunnel-gateway` and
`tunnel-gateway-1` UDP Services and their exact NLB/target-group/security-group IDs;
the legacy/indexed gateway Deployments and bootstrap Job/service account; the
`lazycloud-wireguard` ExternalSecret/Secret; the `wg.lazycloud.dev` DNS record;
the deployment's old key document and bootstrap Pod Identity/IAM resources; and
the old tunnel-gateway ECR repository after its retained releases are no longer
needed. Names describe candidates, not permission to delete unmatched resources.
Database migrations preserve historical revision files and durable tenant data.

## Local composition

Run `bash deploy/setup-local-env.sh` to initialize the missing dedicated bootstrap
credential. `tunnel-issuer-bootstrap` creates the local CA once in `tunnel-issuer`.
The API alone mounts that volume. Each gateway has a separate leaf/key volume.
`connection-ingress` runs HAProxy in TCP mode on host port 443 and resolves gateway
containers through Docker DNS. `tunnels.lazycloud.test` resolves to that proxy inside
Compose. Host-side SDK calls use the published API port. Local API bootstrap uses the configured
HTTP development origin; certificate issuance and tunnel authentication use the
same contracts as production.

```sh
uv run python -m deploy.release
docker compose ps control-plane connection-gateway connection-gateway-1 connection-ingress agent
```

The agent-managed worker shares its agent's network namespace. Worker and sandbox
callbacks reach the agent's local connector and cross the TLS tunnel. The
`container-worker` build profile supplies its image and has no standalone runtime.
Recreating `control-plane` does not recreate either gateway. Do not remove the CA
volume as part of an ordinary image update.

For an isolated local deployment, set `COMPOSE_PROJECT_NAME`, the declared local
ports and state paths, and `LAZYCLOUD_COMPOSE_IMAGE_TAG`. The image tag also selects
the agent-managed worker image so another stack's build cannot replace it.
