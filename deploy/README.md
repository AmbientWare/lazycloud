# LazyCloud Kubernetes Deployments

GitOps-managed Kubernetes resources deployed via ArgoCD.

## Structure

```
deploy/
├── argocd-apps/                    # ArgoCD Application manifests
│   ├── root-app.yaml               # App of Apps entry point
│   └── applicationsets/            # ApplicationSets for multi-cluster
├── platform/                       # Platform Helm charts
│   ├── envoy-gateway/
│   ├── cloudflare-tunnel/
│   ├── karpenter/
│   ├── external-secrets/
│   └── ...
└── services/                       # Application Helm charts
    ├── api-platform/
    └── web/
```

## Setup

After CDK deploys infrastructure:

```bash
# Install ArgoCD
helm repo add argo https://argoproj.github.io/argo-helm
helm install argocd argo/argo-cd -n argocd --create-namespace \
    -f ../infrastructure/argocd-values.yaml --wait --timeout 15m

# Apply root application (starts GitOps sync)
kubectl apply -f argocd-apps/root-app.yaml

# Access ArgoCD UI
kubectl port-forward -n argocd svc/argocd-server 8080:443
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
```

## How It Works

1. `root-app.yaml` watches `deploy/argocd-apps/applicationsets/` for ApplicationSet manifests
2. ApplicationSets generate Applications for each cluster using cluster labels
3. Each chart uses `values.yaml` (shared) + `values-{region}.yaml` (region-specific)

## Adding Components

**New platform component:**
1. Create chart in `deploy/platform/<name>/`
2. Add entry to `deploy/argocd-apps/applicationsets/platform.yaml`
3. If region-specific, create `values-{region}.yaml` and set `hasRegionValues: "true"`

**New service:**
1. Create chart in `deploy/services/<name>/`
2. Add to ApplicationSets in `deploy/argocd-apps/applicationsets/`:
   - `services-staging.yaml`
   - `services-prod.yaml`
3. Create `values-{region}.yaml` for region-specific values (certificates, etc.)

## Cloudflare Tunnel

Each cluster uses its own Cloudflare Tunnel for ingress traffic. Tunnels are **remote-managed** and **fully automated via Terraform**.

### URL Pattern

Each cluster has its own subdomain with per-cluster Advanced SSL certificate:
```
{service}-{deploymentId}.{cluster_id}.lazycloud.dev
```

Example: `api-abc12.ash-1.lazycloud.dev`, `web-xyz99.ash-2.lazycloud.dev`

### How It Works

When you deploy a cluster with Terraform, it automatically:
1. Creates a Cloudflare Tunnel named `lazycloud-prod-{cluster_id}`
2. Configures ingress routes for `*.{cluster_id}.lazycloud.dev` → Envoy Gateway
3. Creates the wildcard DNS CNAME `*.{cluster_id}.lazycloud.dev` → tunnel
4. Creates an Advanced SSL certificate for `*.{cluster_id}.lazycloud.dev` (requires ACM add-on ~$10/mo)
5. Stores the tunnel token in AWS Secrets Manager at `lazycloud/clusters/{cluster_id}`

The ArgoCD-managed `cloudflare-tunnel` chart then deploys `cloudflared` pods that connect to the tunnel using the token from Secrets Manager.

### Prerequisites

Before deploying a cluster, ensure you have:
1. A Cloudflare API token with `Cloudflare Tunnel:Edit` and `DNS:Edit` permissions
2. Your Cloudflare Account ID and Zone ID (from the Cloudflare dashboard)

Set these in your cluster's `terraform.tfvars`:
```hcl
cloudflare_account_id = "your-account-id"
cloudflare_zone_id    = "your-zone-id"
cloudflare_zone       = "lazycloud.dev"
```

And the API token via environment variable:
```bash
export TF_VAR_cloudflare_api_token="your-api-token"
```

### Cluster Values File

Each cluster needs a values file at `deploy/platform/cloudflare-tunnel/values-{cluster_id}.yaml`:
```yaml
tunnel:
  name: "lazycloud-prod-{cluster_id}"
secrets:
  # Points to the cluster's secret in AWS Secrets Manager
  awsSecretName: lazycloud/clusters/{cluster_id}
```

The cluster secret is a JSON object containing both `kubeconfig` and `cloudflare_tunnel_token` properties, automatically created by Terraform.

### How Traffic Flows

```
User → Cloudflare Edge (TLS termination) → Cloudflare Tunnel → Envoy Gateway → Service
```

Cloudflare handles TLS termination at the edge, so traffic between the tunnel and Envoy is HTTP.
