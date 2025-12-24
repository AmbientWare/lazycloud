# Infrastructure Setup

See [architecture.md](architecture.md) for system diagrams.

## Prerequisites

- AWS CLI configured
- kubectl
- helm
- uv (Python package manager)
- cloudflared CLI
- Cloudflare account with Zero Trust access

## Setup

### 1. Deploy CDK Shared Stack

```bash
cd infrastructure
uv sync
uv run cdk deploy lazycloud-shared
```

Creates: IAM roles, ECR repos, Secrets Manager secrets (empty)

### 2. Configure Cloudflare Tunnel

Create tunnel:
```bash
cloudflared tunnel login
cloudflared tunnel create lazycloud-prod
```

Copy the tunnel ID from output and update:
```
deploy/platform/cloudflare-tunnel/values-us-east-1.yaml
```

Get tunnel token and add to AWS Secrets Manager (`lazycloud/shared-secrets`):
```bash
cloudflared tunnel token lazycloud-prod
```

```json
{
  "CLOUDFLARE_TUNNEL_TOKEN": "<token-from-above>"
}
```

Configure DNS in Cloudflare dashboard:
- Add CNAME: `*.lazycloud.dev` → `<tunnel-id>.cfargotunnel.com`
- Add CNAME: `lazycloud.dev` → `<tunnel-id>.cfargotunnel.com`

#### Security Rule for Install Scripts

Cloudflare's Security Level blocks curl/wget requests by default. To allow CLI install scripts:

1. Go to Security → Security rules
2. Create rule with expression: `starts_with(http.request.uri.path, "/install")`
3. Action: **Skip** → check **Security Level**
4. Deploy

#### Cloudflare for SaaS (Custom Domains)

Enable SSL for SaaS in Cloudflare dashboard:
1. Go to SSL/TLS → Custom Hostnames
2. Enable Custom Hostnames (requires paid plan)
3. Set fallback origin: `lazycloud.dev`

Create API token with permissions:
- Zone > SSL and Certificates > Edit
- Zone Resources: Include > Specific zone > lazycloud.dev

Add credentials to `lazycloud/prod-secrets`:
```json
{
  "CLOUDFLARE_API_KEY": "<api-token>",
  "CLOUDFLARE_ZONE_ID": "<zone-id-from-dashboard-overview>",
  "CLOUDFLARE_ACCOUNT_ID": "<account-id-from-dashboard-url>"
}
```

Note: Zone ID is on the domain overview page. Account ID is in the dashboard URL.

These are referenced in `deploy/services/api-platform/values.yaml`.

Customer flow:
1. Customer adds custom domain in app
2. Backend calls `CloudflareService.add_saas_domain()`
3. Customer adds CNAME: `customerdomain.com` → `lazycloud.dev`
4. Cloudflare issues SSL cert automatically

### 3. Deploy CDK Infra Stack

```bash
uv run cdk deploy lazycloud-prod-us-east-1-infra
```

**Note:** Deploy will pause at ACM certificate creation waiting for validation.

While it waits:
1. Open AWS ACM console (us-east-1)
2. Find the pending certificate for `*.lazycloud.dev`
3. Copy the CNAME name and value
4. Add CNAME record in Cloudflare DNS
5. Deploy will continue once validated (< 5 min)

Creates: VPC, EKS cluster, EFS, ACM wildcard cert, Pod Identity associations

### 4. Configure kubectl

```bash
aws eks update-kubeconfig --region us-east-1 --name lazycloud-prod-us-east-1-eks
```

### 5. Populate App Secrets

Add to AWS Secrets Manager (`lazycloud/prod-secrets`):
```json
{
  "DATABASE_URL": "...",
  "REDIS_URL": "...",
  "SECRET_KEY": "..."
}
```

### 6. Install ArgoCD

```bash
helm repo add argo https://argoproj.github.io/argo-helm
helm install argocd argo/argo-cd -n argocd --create-namespace \
  -f infrastructure/argocd-values.yaml --wait --timeout 15m
```

Get admin password:
```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
```

### 7. Deploy Root Application

```bash
kubectl apply -f deploy/argocd-apps/root-app.yaml
```

This triggers the full platform deployment via sync waves:
1. storage-classes, reloader
2. external-secrets
3. nginx-ingress
4. cloudflare-tunnel, karpenter
5. loki
6. prometheus-stack
7. services (prod/staging)

## Teardown

Reverse order:

```bash
# Remove k8s resources
kubectl delete -f deploy/argocd-apps/root-app.yaml
helm uninstall argocd -n argocd
kubectl delete ns argocd

# Destroy AWS infrastructure
cd infrastructure
uv run cdk destroy lazycloud-prod-us-east-1-infra
uv run cdk destroy lazycloud-shared

# Remove Cloudflare tunnel (optional)
cloudflared tunnel delete lazycloud-prod
# Also remove DNS CNAMEs from Cloudflare dashboard
```

## Key Files

| Component | Location |
|-----------|----------|
| CDK config | `infrastructure/src/infrastructure/config/environments.py` |
| ArgoCD values | `infrastructure/argocd-values.yaml` |
| Platform apps | `deploy/argocd-apps/applicationsets/platform.yaml` |
| Service apps | `deploy/argocd-apps/applicationsets/services-*.yaml` |
| Cloudflare tunnel | `deploy/platform/cloudflare-tunnel/` |
| Cloudflare SaaS service | `apps/backend/src/backend/services/cloudflare.py` |
| API platform config | `deploy/services/api-platform/values.yaml` |
| Karpenter | `deploy/platform/karpenter/` |
