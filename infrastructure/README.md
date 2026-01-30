# Hetzner Infrastructure

## Prerequisites
- Hetzner Cloud account with API token (read/write)
- JuiceFS Cloud account (metadata service) + AWS S3 bucket (data)
- Cloudflare account with API token:
  - **Account permissions**: Cloudflare Tunnel:Edit
  - **Zone permissions**: DNS:Edit, SSL and Certificates:Edit
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
export TF_VAR_cloudflare_api_token="your-cloudflare-token"
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
- Cluster secrets stored in AWS Secrets Manager (`lazycloud/clusters/{cluster_id}`)
  - Contains: `kubeconfig` + `cloudflare_tunnel_token`
- ArgoCD (with ingress at `argocd.{cluster_id}.lazycloud.dev`)
- Cloudflare Tunnel (auto-created with DNS `*.{cluster_id}.lazycloud.dev` → tunnel)
- Advanced SSL Certificate for `*.{cluster_id}.lazycloud.dev` (requires ACM add-on ~$10/mo)

First, update `terraform.tfvars` with your Cloudflare IDs:
```hcl
cloudflare_account_id = "your-account-id"  # From Cloudflare dashboard
cloudflare_zone_id    = "your-zone-id"     # From zone's API section
```

Then apply:
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

## 8. Verify Deployment and Access ArgoCD
Cloudflare tunnel and DNS are automatically configured by Terraform.
```bash
# Get cluster status and ArgoCD password
./infrastructure/cluster-info.sh
```
- Login to ArgoCD at https://argocd.{cluster_id}.lazycloud.dev (e.g., `argocd.ash-1.lazycloud.dev`) with the displayed admin password
- Verify platform apps are syncing in the ArgoCD dashboard

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

# 3. Force delete the cluster secret from AWS (it may be scheduled for deletion)
aws secretsmanager delete-secret \
  --secret-id lazycloud/clusters/ash-1 \
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

Each cluster gets its own subdomain: `*.{cluster_id}.lazycloud.dev`
Terraform automatically creates the DNS record and Advanced SSL certificate.

To add a second cluster (e.g., `ash-2` or `fsn-1`):

1. Create cluster directory:
   ```bash
   cp -r clusters/ash-1 clusters/ash-2
   ```

2. Update `clusters/ash-2/terraform.tfvars`:
   - Change network CIDRs (must not overlap)
   - Update cluster name and ID
   - Cloudflare settings are inherited (same zone)

3. Update `clusters/ash-2/backend.hcl`:
   - Change state key to `clusters/ash-2/terraform.tfstate`

4. Create cloudflare-tunnel values file:
   ```bash
   # Create deploy/platform/cloudflare-tunnel/values-{cluster_id}.yaml
   cat > deploy/platform/cloudflare-tunnel/values-ash-2.yaml << 'EOF'
   tunnel:
     name: "lazycloud-prod-ash-2"
   secrets:
     awsSecretName: lazycloud/clusters/ash-2
   EOF
   ```

5. Add to cluster registry:
   - Edit `packages/configs/src/configs/clusters.yaml`
   - Add new cluster with unique CIDRs

6. Apply:
   ```bash
   cd clusters/ash-2
   terraform init -backend-config=backend.hcl
   terraform apply
   ```

7. The backend will automatically load the new cluster's kubeconfig from Secrets Manager on restart.

## Multi-Cluster Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  AWS Secrets Manager                                                │
├─────────────────────────────────────────────────────────────────────┤
│  Global (from terraform/global):                                    │
│    lazycloud/prod-secrets      - Production app secrets             │
│    lazycloud/shared-secrets    - Depot token, etc.                  │
│    lazycloud/staging-secrets   - Staging app secrets                │
│                                                                     │
│  Per-Cluster (from terraform/clusters/* - JSON object):             │
│    lazycloud/clusters/ash-1                                         │
│      ├─ kubeconfig              (for backend K8s client)            │
│      └─ cloudflare_tunnel_token (for cloudflared pods)              │
│    lazycloud/clusters/ash-2     (when added)                        │
│      ├─ kubeconfig                                                  │
│      └─ cloudflare_tunnel_token                                     │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  Cloudflare (auto-configured by Terraform per cluster)              │
├─────────────────────────────────────────────────────────────────────┤
│  Cluster: ash-1                                                     │
│    Tunnel: lazycloud-prod-ash-1                                     │
│      └─ Ingress: *.ash-1.lazycloud.dev → nginx-ingress-controller   │
│    DNS: *.ash-1.lazycloud.dev → tunnel CNAME                        │
│    SSL: Advanced Certificate for *.ash-1.lazycloud.dev              │
│  Cluster: ash-2 (when added)                                        │
│    Tunnel: lazycloud-prod-ash-2                                     │
│      └─ Ingress: *.ash-2.lazycloud.dev → nginx-ingress-controller   │
│    DNS: *.ash-2.lazycloud.dev → tunnel CNAME                        │
│    SSL: Advanced Certificate for *.ash-2.lazycloud.dev              │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  Backend Startup                                                    │
├─────────────────────────────────────────────────────────────────────┤
│  1. Reads clusters.yaml to get list of active clusters              │
│  2. For each cluster, fetches secrets JSON from Secrets Manager     │
│  3. Extracts kubeconfig and creates K8s client per cluster          │
│  4. Operations use deployment.cluster_id to route to correct client │
└─────────────────────────────────────────────────────────────────────┘
```
