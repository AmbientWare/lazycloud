# LazyCloud Infrastructure - AWS CDK

## Architecture

**2-Stack Architecture** with manual ArgoCD installation:

```
CDK (AWS Resources)              Manual (K8s Bootstrap)
────────────────────             ─────────────────────
lazycloud-shared
  └─ IAM, ECR, Secrets

lazycloud-prod-infra
  └─ VPC, EKS, Pod Identity
  └─ ConfigMap ──────────────────→ helm install argocd
                                   kubectl apply root-app.yaml
                                         │
                                         └─→ ArgoCD syncs everything
                                              ├─ aws-load-balancer-controller
                                              ├─ karpenter
                                              ├─ external-secrets
                                              ├─ prometheus-stack
                                              └─ services
```

## Quick Start

```bash
cd infrastructure
uv sync

# Deploy stacks
uv run cdk deploy lazycloud-shared
uv run cdk deploy lazycloud-prod-us-east-1-infra

# Configure kubectl
aws eks update-kubeconfig --region us-east-1 --name lazycloud-prod-us-east-1-eks

# Install ArgoCD
helm repo add argo https://argoproj.github.io/argo-helm
helm install argocd argo/argo-cd -n argocd --create-namespace \
    -f argocd-values.yaml --wait --timeout 15m

# Apply root application (starts GitOps sync)
kubectl apply -f ../deploy/argocd-apps/root-app.yaml

# Access ArgoCD
kubectl port-forward -n argocd svc/argocd-server 8080:443
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
```

## Project Structure

```
infrastructure/
├── main.py                           # CDK app entry point
├── argocd-values.yaml                # ArgoCD Helm values
└── src/infrastructure/
    ├── config/environments.py        # Environment configs
    ├── stacks/
    │   ├── shared/                   # IAM, ECR, Secrets
    │   └── prod/infra_stack.py       # VPC + EKS + Pod Identity + ConfigMap
    └── constructs/
        ├── aws/                      # VPC, EKS, ECR
        ├── iam/                      # IAM roles
        └── components/
            ├── infrastructure.py     # VPC + EKS
            └── controllers.py        # Pod Identity + ConfigMap
```

## Cleanup

```bash
# Remove ArgoCD first
helm uninstall argocd -n argocd

# Destroy stacks
uv run cdk destroy lazycloud-prod-us-east-1-infra
uv run cdk destroy lazycloud-shared
```
