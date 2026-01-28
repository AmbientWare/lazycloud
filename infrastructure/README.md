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
# Kubeconfig (also stored in Secrets Manager for backend)
terraform output -raw kubeconfig > ~/.kube/lazycloud
export KUBECONFIG=~/.kube/lazycloud

# Talos config (for node management)
terraform output -raw talosconfig > ~/.talos/config
```

Or use the helper script:
```bash
source ./infrastructure/kubesetup.sh
```

## 7. Deploy Platform via ArgoCD
The ApplicationSet deploys all platform charts from `deploy/platform/`.
```bash
kubectl apply -f deploy/argocd-apps/applicationsets/platform.yaml -n argocd
```

## 8. Update Billing
Creates/updates meters and products in Polar:
```bash
uv run update-billing
```

## 9. DNS Cutover
- Update Cloudflare tunnel to point to new cluster's NGINX ingress
- Run `./infrastructure/validate.sh` to get the ingress IP
- Verify: `curl -I https://lazycloud.dev`

## 10. Verify
- [ ] Nodes ready (`kubectl get nodes`)
- [ ] Pods running with gVisor (`kubectl get runtimeclass`)
- [ ] JuiceFS CSI pods running (`kubectl get pods -n kube-system -l app=juicefs-csi-driver`)
- [ ] Storage classes created (`kubectl get sc`)
- [ ] ArgoCD accessible (`kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d`)
- [ ] Kubeconfig in Secrets Manager (`aws secretsmanager get-secret-value --secret-id lazycloud/clusters/ash-1/kubeconfig`)
- [ ] Deploy a test workspace
- [ ] Confirm builds (Depot), storage (PVCs), ingress (public URL)

## Teardown
```bash
# Destroy cluster first
cd infrastructure/terraform/clusters/ash-1
terraform destroy

# Optionally destroy global resources (deletes all shared secrets!)
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
