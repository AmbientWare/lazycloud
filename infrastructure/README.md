# Hetzner Infrastructure

## Prerequisites
- Hetzner Cloud account with API token (read/write)
- JuiceFS Cloud account (metadata service) + AWS S3 bucket (data)
- Terraform >= 1.8
- AWS account with S3 (for Terraform state) and SecretsManager access
- helm, kubectl installed

## Directory Structure
```
infrastructure/terraform/
├── global/                 # Shared resources (secrets) - deploy once
│   ├── main.tf
│   ├── variables.tf
│   ├── secrets.tf          # Shared app secrets (prod, staging, shared)
│   └── backend.hcl
├── clusters/
│   └── ash-1/              # US East (Ashburn) cluster
│       ├── main.tf         # Module call + providers
│       ├── variables.tf    # Input variables
│       ├── terraform.tfvars # Non-sensitive values
│       └── backend.hcl     # S3 backend config
└── modules/
    └── hetzner-cluster/    # Reusable cluster module
```

## 1. Create S3 Bucket for State (one-time)
```bash
aws s3 mb s3://lazycloud-terraform-state --region us-east-1
```

## 2. Set Environment Variables
Sensitive values are passed via environment variables:
```bash
export TF_VAR_hcloud_token="your-hetzner-token"
export TF_VAR_aws_access_key_id="your-aws-key"
export TF_VAR_aws_secret_access_key="your-aws-secret"
export TF_VAR_juicefs_token="your-juicefs-token"
```

## 3. Provision Global Resources (one-time)
Creates shared secret containers in AWS Secrets Manager:
```bash
cd infrastructure/terraform/global
terraform init -backend-config=backend.hcl
terraform apply
```

## 4. Populate Shared Secrets
Terraform creates empty secret containers. Populate them:
```bash
aws secretsmanager put-secret-value --secret-id lazycloud/prod-secrets \
  --secret-string file://infrastructure/secrets-backup/lazycloud-prod-secrets.json

aws secretsmanager put-secret-value --secret-id lazycloud/shared-secrets \
  --secret-string file://infrastructure/secrets-backup/lazycloud-shared-secrets.json

aws secretsmanager put-secret-value --secret-id lazycloud/staging-secrets \
  --secret-string file://infrastructure/secrets-backup/lazycloud-staging-secrets.json
```

## 5. Provision Cluster
Single `terraform apply` provisions the Hetzner cluster **and** bootstraps all platform components:
- Talos Linux control plane + workers
- Cilium CNI
- JuiceFS CSI driver + credentials
- Storage classes (`juicefs-standard`, `juicefs-shared`)
- gVisor RuntimeClass
- Cluster Autoscaler
- Kubeconfig stored in AWS Secrets Manager (`lazycloud/clusters/ash-1/kubeconfig`)
- ArgoCD (with ingress at `argocd.lazycloud.dev`)

```bash
cd infrastructure/terraform/clusters/ash-1
terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

## 6. Get Credentials (for local access)
```bash
source ./infrastructure/kubesetup.sh
```

## 7. Deploy Platform via ArgoCD
The ApplicationSet deploys all platform charts from `deploy/platform/`.
```bash
kubectl apply -f deploy/argocd-apps/applicationsets/platform.yaml -n argocd
```

## 8. DNS Cutover and Argo dashboard
- Update Cloudflare tunnel to point to new cluster's NGINX ingress
- Get the cluster status and info:
```bash
./infrastructure/cluster-info.sh
```
- Update the dns CNAMEs shown as 'needed'
- use the displayed argo Admin password to login to https://argocd.lazycloud.dev

## 9. Update Billing
Creates/updates meters and products in Polar:
```bash
uv run update-billing
```

## 10. If deploying the LazyCloud main apps:
- Run the Staging and Production actions in GitHub (builds container images)
- Then add the apps to ArgoCD:
```bash
kubectl apply -f deploy/argocd-apps/applicationsets/services-staging.yaml -n argocd
kubectl apply -f deploy/argocd-apps/applicationsets/services-prod.yaml -n argocd
```


## Teardown

### Clean Destroy (recommended)
The cluster has Kubernetes/Helm resources that can't be deleted once the API server is gone.
Use the `skip_bootstrap` variable to cleanly remove them from Terraform state first:

```bash
cd infrastructure/terraform/clusters/ash-1

# Step 1: Remove K8s/Helm resources from Terraform management
terraform apply -var="skip_bootstrap=true"

# Step 2: Destroy infrastructure (only Hetzner resources remain)
terraform destroy
```

### Full Reset (start completely fresh)
If you need to completely reset and start over:

```bash
cd infrastructure/terraform/clusters/ash-1

# 1. Remove K8s resources from state (skip if cluster is already dead)
terraform apply -var="skip_bootstrap=true" || true

# 2. Destroy infrastructure
terraform destroy || true

# 3. Force delete the kubeconfig secret from AWS (it may be scheduled for deletion)
aws secretsmanager delete-secret \
  --secret-id lazycloud/clusters/ash-1/kubeconfig \
  --force-delete-without-recovery

# 4. Clear Terraform state from S3
aws s3 rm s3://lazycloud-terraform-state/clusters/ash-1/terraform.tfstate

# 5. Reinitialize and apply fresh
terraform init -backend-config=backend.hcl
terraform apply
```

### Destroy Global Resources
Only do this if you want to delete ALL shared secrets:
```bash
cd infrastructure/terraform/global
terraform destroy
```

## Adding a New Cluster
To add a second cluster (e.g., `ash-2` or `fsn-1`):

1. Create cluster directory:
   ```bash
   cp -r clusters/ash-1 clusters/ash-2
   ```

2. Update `clusters/ash-2/terraform.tfvars`:
   - Change network CIDRs (must not overlap)
   - Update cluster name and ID

3. Update `clusters/ash-2/backend.hcl`:
   - Change state key to `clusters/ash-2/terraform.tfstate`

4. Add to cluster registry:
   - Edit `packages/configs/src/configs/clusters.yaml`
   - Add new cluster with unique CIDRs

5. Apply:
   ```bash
   cd clusters/ash-2
   terraform init -backend-config=backend.hcl
   terraform apply
   ```

6. The backend will automatically load the new cluster's kubeconfig from Secrets Manager on restart.

## Multi-Cluster Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  AWS Secrets Manager                                                │
├─────────────────────────────────────────────────────────────────────┤
│  Global (from terraform/global):                                    │
│    lazycloud/prod-secrets      - Production app secrets             │
│    lazycloud/shared-secrets    - Cloudflare, Depot, etc.            │
│    lazycloud/staging-secrets   - Staging app secrets                │
│                                                                     │
│  Per-Cluster (from terraform/clusters/*):                           │
│    lazycloud/clusters/ash-1/kubeconfig                              │
│    lazycloud/clusters/ash-2/kubeconfig  (when added)                │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  Backend Startup                                                    │
├─────────────────────────────────────────────────────────────────────┤
│  1. Reads clusters.yaml to get list of active clusters              │
│  2. For each cluster, fetches kubeconfig from Secrets Manager       │
│  3. Creates K8s client per cluster                                  │
│  4. Operations use deployment.cluster_id to route to correct client │
└─────────────────────────────────────────────────────────────────────┘
```
