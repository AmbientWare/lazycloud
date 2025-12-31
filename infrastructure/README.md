# LazyCloud Infrastructure - AWS CDK

## Architecture

**2-Stack Architecture** with manual ArgoCD installation:

```
CDK (AWS Resources)              Manual (K8s Bootstrap)
────────────────────             ─────────────────────
lazycloud-shared
  └─ IAM, Secrets

lazycloud-prod-infra
  └─ VPC, EKS, Pod Identity
  └─ ConfigMap ──────────────────→ helm install argocd
                                   kubectl apply root-app.yaml
                                         │
                                         └─→ ArgoCD syncs everything
                                              ├─ nginx-ingress
                                              ├─ cloudflare-tunnel
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
    │   ├── shared/                   # IAM, Secrets
    │   └── prod/infra_stack.py       # VPC + EKS + Pod Identity + ConfigMap
    └── constructs/
        ├── aws/                      # VPC, EKS
        ├── iam/                      # IAM roles
        └── components/
            ├── infrastructure.py     # VPC + EKS
            └── controllers.py        # Pod Identity + ConfigMap
```

## gVisor AMI (for Karpenter sandbox nodes)

The CDK stack creates an EC2 Image Builder pipeline for building AL2023 AMIs with gVisor pre-installed. This eliminates race conditions when provisioning sandbox nodes.

### Build the AMI (automated)

```bash
./scripts/build_gvisor_ami.sh
git add -A && git commit -m "Update gVisor AMI" && git push
kubectl delete nodes -l runtime=gvisor
```

### Build the AMI (manual)

```bash
# 1. Start the build (takes ~15-20 minutes)
aws imagebuilder start-image-pipeline-execution --image-pipeline-arn \
  $(aws imagebuilder list-image-pipelines --query "imagePipelineList[?name=='lazycloud-gvisor-pipeline'].arn" --output text)

# 2. Check build status
aws imagebuilder list-images --owner Self --query "imageVersionList[?contains(name, 'gvisor')]" --output table

# 3. Get the AMI ID when complete
aws ec2 describe-images --owners self --filters "Name=tag:Runtime,Values=gvisor" --query "Images | sort_by(@, &CreationDate) | [-1].ImageId" --output text

# 4. Update deploy/platform/karpenter/values-us-east-1.yaml with gvisorAmiId
# 5. git push && kubectl delete nodes -l runtime=gvisor
```

## Cleanup

```bash
# Remove ArgoCD first
helm uninstall argocd -n argocd

# Destroy stacks
uv run cdk destroy lazycloud-prod-us-east-1-infra
uv run cdk destroy lazycloud-shared
```
