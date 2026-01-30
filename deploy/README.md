# LazyCloud Kubernetes Deployments

GitOps-managed Kubernetes resources deployed via ArgoCD.

## Structure

```
deploy/
├── argocd-apps/                    # ArgoCD Application manifests
│   ├── root-app.yaml               # App of Apps entry point
│   └── applicationsets/            # ApplicationSets for multi-cluster
├── platform/                       # Platform Helm charts
│   ├── nginx-ingress/
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

The cluster uses a Cloudflare Tunnel for ingress traffic. The tunnel is **remote-managed**, meaning routing configuration lives in the Cloudflare Zero Trust dashboard.

### Setup

1. **Create tunnel** in [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) > Networks > Tunnels
2. **Copy the token** from the tunnel install command
3. **Store token** in AWS Secrets Manager at `lazycloud/shared-secrets` with key `CLOUDFLARE_TUNNEL_TOKEN`
4. **Configure routes** in the tunnel's "Public Hostname" tab:
   - `lazycloud.dev` → `http://nginx-ingress-controller.ingress-nginx.svc.cluster.local:80`
   - `*.lazycloud.dev` → `http://nginx-ingress-controller.ingress-nginx.svc.cluster.local:80`

### How Traffic Flows

```
User → Cloudflare Edge (TLS termination) → Cloudflare Tunnel → NGINX Ingress → Service
```

Cloudflare handles TLS termination at the edge, so traffic between the tunnel and NGINX is HTTP.
