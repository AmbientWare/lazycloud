# Local Cluster

Use the Helm chart for a functional local Kubernetes LazyCloud stack.

```sh
kubectl create namespace lazycloud
helm upgrade --install lazycloud deploy/charts/lazycloud \
  --namespace lazycloud \
  --values deploy/charts/lazycloud/values.local.yaml
```

The local stack includes the current-schema database bootstrap job, control plane, scheduler,
worker, container-worker, Postgres, Redis, S3-compatible object storage, object
bucket initialization, cache service, RBAC, and monitoring resources.
Container-worker pods run privileged because local worker containers need
namespace, network, mount, and image-build capabilities.

`values.local.yaml` enables the scheduler-owned Kubernetes worker-pool scaler.
The scheduler patches `lazycloud-container-worker-*` Deployments when durable pool
sizing labels request more worker capacity.

Environment preparation is an operator action. Create a disposable cluster,
load the already-built images, and install the chart using the normal k3d,
Docker, Helm, and kubectl commands for that environment. Do not hide those
mutations inside an acceptance test.

After the release is ready and its API is reachable (for example through a
separately managed `kubectl port-forward`), export its exact target:

```sh
export LAZYCLOUD_E2E_KUBERNETES_CONTEXT=<context>
export LAZYCLOUD_E2E_KUBERNETES_NAMESPACE=lazycloud
export LAZYCLOUD_E2E_KUBERNETES_RELEASE=lazycloud
export LAZYCLOUD_ENDPOINT=http://127.0.0.1:8000
export LAZYCLOUD_TOKEN=<temporary-workspace-token>
```

Run only the exact Kubernetes scenario required by the changed capability. Each
scenario blocks on its own preconditions—the public endpoint's health and the
prepared worker pool's ready baseline—so a release that is not actually serving
reports itself as blocked rather than failing the scenario. Worker-pod
interruption and scheduler rollout restart are explicit operator actions; the
corresponding post-action scenarios accept concrete resource identities and
never delete the cluster, namespace, release, or pod.
