# LazyCloud Kubernetes Deployments

GitOps-managed Kubernetes resources deployed via ArgoCD.

## Structure

```
deploy/
├── argocd-apps/                    # ArgoCD Application manifests
│   ├── root-app.yaml               # App of Apps entry point
│   ├── platform/                   # Platform applications
│   └── *-prod.yaml, *-staging.yaml # Service applications
├── platform/                       # Platform Helm charts
│   ├── karpenter/
│   ├── external-secrets/
│   ├── prometheus-stack/
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

1. `root-app.yaml` watches `deploy/argocd-apps/` for Application manifests
2. ArgoCD syncs all platform and service apps automatically
3. Platform apps get infrastructure values from ConfigMap at `/infrastructure-values/`

## Adding Components

**New platform component:**
1. Create chart in `deploy/platform/<name>/`
2. Create Application in `deploy/argocd-apps/platform/<name>.yaml`
3. If needs infra values, add to CDK ConfigMap in `controllers.py`

**New service:**
1. Create chart in `deploy/services/<name>/`
2. Create Applications in `deploy/argocd-apps/`:
   - `<name>-staging.yaml`
   - `<name>-prod.yaml`
