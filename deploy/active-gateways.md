# Active gateways

Implementation branch: `feat/active-gateways`.

This branch describes the final deployment. It must not replace the deployed
chart in one sync. The Service-only provisioning stage has created gateway one's
NLB and its assigned endpoint is recorded in the production environment. The
application migration still requires the ordered releases below.

## Outcome

Two independently addressed gateways serve traffic at once. Agents and API peers
keep a tunnel to each gateway. A gateway deployment stops receiving new
connections before its existing connections drain. Worker addresses and enrollment
identity survive gateway and API deployments.

PostgreSQL owns gateway indices, public keys, endpoints and enrolled peers. Redis
leases fence processes sharing a gateway identity. Each gateway publishes the
peer identities and generations that pass a fresh encrypted TCP probe under that
lease, with a ten-second TTL. Platform clients require this presence and their
own encrypted gateway probe before selecting a path to that peer. Historical
handshakes and database records alone never prove a working tunnel.

## Implementation

1. Add an indexed gateway registry through an additive Alembic migration. Preserve
   gateway 0's existing identity, private key and public Service.
2. Add the canonical private-network registration and topology contracts. A
   topology refresh reads enrollment authority without resetting peer generation
   or worker readiness. Repeated registration with the same key is idempotent.
3. Establish one WireGuard interface per gateway, using the peer's stable inner
   address. Linux connection marks retain each connection's gateway in both
   directions. Separate routing tables prevent overlapping tunnel routes from
   displacing one another. Only new connections select another healthy gateway.
4. Scope gateway leases to identities. Drain on shutdown while retaining the
   interface and existing forwarding state. Health responses distinguish accepting
   new connections from draining. API readiness requires one usable path.
5. Give each gateway its own UDP Service and key. Roll one identity at a time.
   The second Service adds one NLB. Record its assigned hostname in the deployment
   environment after provisioning; workloads do not query the Kubernetes API.

## Migration

Installed agents reject unknown JSON fields and register their private network
before checking for updates. A single incompatible response change strands them.
The migration therefore needs ordered releases, with one canonical implementation
behind the endpoints consumed by each installed version.

Each release below needs its own reviewed source commit, completed API rollout
and active-release confirmation. Ship accepts a selected commit only after it is
merged into `main`.

1. Prepare the server and updater. Add migration `0043`, indexed registry reads,
   canonical registration and topology endpoints. Keep the existing registration
   response exact. Ship the existing agent protocol and networking runtime, with
   the updater cleanup fix below. Publish the new network image and start only
   gateway 1, using local readiness for this preparation release. Freeze gateway
   0's Deployment template and old platform sidecar configuration at their
   currently deployed immutable network image digest. Keep gateway 0's Service
   selector, global lease and `server-public-key` projection. The shared image
   helper must not upgrade these old consumers to the new image.
2. Confirm every API replica serves the canonical contracts and every authorized
   agent has the preparation binary. Then publish the new agent runtime and
   remove its old local networking implementation. Keep both registration
   endpoints on every API replica. Keep gateway 1 on the same pod and image,
   and leave gateway 0 and platform sidecars frozen. New agents establish both
   tunnels but select gateway 1: the deployed gateway 0 closes its health socket
   without the `ready` response required by the new client.
3. Verify each authorized agent runs the new binary and gateway 1 can probe its
   route proxy over WireGuard. Roll API pods with the native platform sidecar,
   PostgreSQL registry and Redis presence. Keep gateway 1's pod and image unchanged
   throughout this rollout. Gateway 1 carries new platform-to-agent connections
   because the old gateway 0 publishes no presence. Verify the gateway-one path
   and a real agent request from every platform peer.
4. Apply the final chart and handshake-gated gateway readiness. Replace gateway
   0's shared Deployment while gateway 1 serves traffic. Preserve gateway 0's key
   and Service object. Prove gateway 0 through the public NLB before its wave
   completes, then let Argo roll gateway 1. Remove the old registration endpoint,
   singular contracts, image pins, server public-key projection and global lease.

The existing updater uses `os.execv`, which skips the daemon's `finally` block
and leaves its old WireGuard interface and routes in the host namespace. The
preparation binary must close owned resources immediately before exec, after
the replacement binary and rollback marker are durable. Upgrading directly to
the new network runtime leaves routes it cannot safely claim. Confirm this
bridge binary on every authorized enrollment before publishing the new runtime.
An offline enrollment remains a migration blocker until it upgrades or its owner
chooses an explicit reinstall or revocation.

The public installer serves each API pod's bundled binary, so active-release
gating alone does not make a mixed-version API rollout safe. The preparation
release is required for fresh installations too.

Do not restart or update gateway 1 between the client and platform stages. It is
the only path the new agents accept until gateway 0 is replaced. Rollback from the
new agent runtime to the old one also requires graceful network cleanup; forcing
a process kill can leave interfaces unknown to the old binary. Keep the old
endpoint until the migration inventory and rollback window are closed.

### Service provisioning stage

The first GitOps commit keeps the deployed gateway Deployment, Service selector,
server key projection and platform sidecars unchanged. It adds only this Service
from the final chart. There is no gateway-one pod yet, so an empty target group
is expected during this stage.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: tunnel-gateway-1
  annotations:
    argocd.argoproj.io/sync-wave: "-2"
    service.beta.kubernetes.io/aws-load-balancer-scheme: internet-facing
    service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: ip
    service.beta.kubernetes.io/aws-load-balancer-healthcheck-protocol: TCP
    service.beta.kubernetes.io/aws-load-balancer-healthcheck-port: "8080"
    service.beta.kubernetes.io/aws-load-balancer-attributes: load_balancing.cross_zone.enabled=true
    service.beta.kubernetes.io/aws-load-balancer-target-group-attributes: deregistration_delay.timeout_seconds=150,deregistration_delay.connection_termination.enabled=false
spec:
  type: LoadBalancer
  loadBalancerClass: eks.amazonaws.com/nlb
  externalTrafficPolicy: Local
  publishNotReadyAddresses: true
  selector:
    app: tunnel-gateway-1
  ports:
    - name: wireguard
      port: 51820
      targetPort: wireguard
      protocol: UDP
```

Record gateway zero's Service UID before the change. Poll both Services, their
events and AWS load-balancer state until gateway one's assigned hostname appears.
Keep gateway zero's UID, selector, endpoint and healthy target unchanged. Record
the new hostname followed by `:51820` under index one in
`deploy/chart/environments/prod.yaml`. The deployment workflow regenerates values
from this source file; an edit only to `values-deployment.yaml` would be lost.

The next prepared release starts gateway one and adds its key while the old
gateway serves gateway zero. Keep `server-public-key` projected until every old
platform sidecar has migrated to registry discovery. Keep the old gateway
Deployment and its Service selector through agent and platform migration. Then
start `tunnel-gateway-0`, select it with the existing `tunnel-gateway` Service,
and retire the old Deployment. The final chart has no migration switch and no
temporary Deployment.

Final gateway Deployments use disjoint `app` labels and share the
`app.kubernetes.io/component=tunnel-gateway` label for their disruption budget and
spreading. Argo sync waves roll gateway zero before gateway one. Each preStop
hook invokes `python -m tunnel_gateway_app.drain`; its 120-second drain fits inside
the 150-second pod grace and NLB deregistration interval.

The final Service publishes pod addresses before readiness so the NLB can
register the new target. The gateway starts its TCP health listener once the
interface and registry entry exist. Pod readiness then requires a fresh encrypted
probe from every configured platform peer through that gateway's public endpoint.
Argo permits the next
gateway wave only after another 35 seconds of readiness. The preparation release
must retain local readiness until new platform clients exist, or gateway one
cannot finish its first rollout.

## Acceptance

Use real Linux WireGuard, routing and conntrack in isolated Docker network
namespaces. Exercise worker-to-API RPC and API-to-worker streams through both
gateways, with different initial path preferences at each end. Prove that replies
return through the incoming gateway, an unchanged topology preserves connections,
draining moves new requests, and the surviving gateway carries requests during a
restart. Inspect routes, handshakes and request outcomes every cycle. Remove only
the containers, network and key files created by this scenario.

Before production promotion, run continuous function calls and a stream across
gateway and API rollouts. Correlate assignment, container startup, request latency,
gateway health and NLB target health. Confirm EKS Auto Mode registers the published
pod endpoint before pod readiness, then observe its platform handshake and the
next Argo wave. Healthy pods alone are insufficient evidence.

A sudden gateway failure loses its local NAT connection state. Existing streams
may need to reconnect. Do not replay request bodies to conceal that failure.
Planned drains must finish before the termination deadline; long connections that
outlive that deadline remain an explicit limit.
