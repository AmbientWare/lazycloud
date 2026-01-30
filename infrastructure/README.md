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

# Optional: Route root domain through this cluster (only enable on ONE cluster)
route_root_domain = true  # Routes lazycloud.dev and *.lazycloud.dev
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

## 7. Register Cluster with ArgoCD

ArgoCD uses cluster secrets to know which clusters to deploy to. The first cluster (hub) needs to be registered so ApplicationSets can target it.

```bash
# Register the first cluster (uses in-cluster API since ArgoCD runs here)
kubectl apply -f deploy/argocd-apps/applicationsets/local-cluster.yaml
```

The cluster secret contains labels that ApplicationSets use:
- `provider: hetzner` - matches platform ApplicationSet selector
- `cluster_id: ash-1` - used for cluster-specific values files (`values-ash-1.yaml`)
- `is-hub: "true"` - identifies this as the ArgoCD hub cluster

## 8. Deploy Platform via ArgoCD
The ApplicationSet deploys all platform charts from `deploy/platform/` to clusters with `provider: hetzner` label.
```bash
kubectl apply -f deploy/argocd-apps/applicationsets/platform-hetzner.yaml -n argocd
```

## 9. Verify Deployment and Access ArgoCD
Cloudflare tunnel and DNS are automatically configured by Terraform.
```bash
# Get cluster status and ArgoCD password
./infrastructure/cluster-info.sh
```
- Login to ArgoCD at https://argocd.{cluster_id}.lazycloud.dev (e.g., `argocd.ash-1.lazycloud.dev`) with the displayed admin password
- Verify platform apps are syncing in the ArgoCD dashboard

## 10. Update Billing
Creates/updates meters and products in Polar:
```bash
uv run update-billing
```

## 11. Deploy LazyCloud Apps (optional)
If deploying the LazyCloud main apps:
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
   - Change `cluster_name` and ensure unique network CIDRs
   - Set `route_root_domain = false` (only one cluster should route root domain)
   - Cloudflare account/zone settings stay the same

3. Update `clusters/ash-2/backend.hcl`:
   - Change state key to `clusters/ash-2/terraform.tfstate`

4. Create cloudflare-tunnel values file:
   ```bash
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
   - `tunnel_id` is optional, can be added after Terraform apply:
     ```bash
     # After terraform apply (output is in state)
     terraform output cloudflare_tunnel_id

     # Or from existing state before apply updates outputs
     terraform state show 'module.cluster.cloudflare_zero_trust_tunnel_cloudflared.cluster_tunnel' | grep ' id '
     ```

6. Apply Terraform:
   ```bash
   cd clusters/ash-2
   terraform init -backend-config=backend.hcl
   terraform apply
   ```

7. Register cluster with ArgoCD (hub-spoke model):
   ArgoCD on ash-1 manages all clusters. Create a cluster secret for the remote cluster:
   ```bash
   # Get the control plane VIP from terraform output
   cd clusters/ash-2
   terraform output control_plane_vip

   # Get kubeconfig credentials from Secrets Manager
   aws secretsmanager get-secret-value --secret-id lazycloud/clusters/ash-2 \
     --query SecretString --output text | jq -r '.kubeconfig'
   ```

   Create `deploy/argocd-apps/applicationsets/cluster-ash-2.yaml`:
   ```yaml
   apiVersion: v1
   kind: Secret
   metadata:
     name: cluster-ash-2
     namespace: argocd
     labels:
       argocd.argoproj.io/secret-type: cluster
       provider: hetzner
       cluster_id: ash-2
       region: us-east-1
       is-hub: "false"
   type: Opaque
   stringData:
     name: ash-2
     server: https://<control-plane-vip>:6443
     config: |
       {
         "tlsClientConfig": {
           "caData": "<base64-ca-from-kubeconfig>",
           "certData": "<base64-cert-from-kubeconfig>",
           "keyData": "<base64-key-from-kubeconfig>"
         }
       }
   ```

   Apply to ArgoCD (on ash-1):
   ```bash
   kubectl apply -f deploy/argocd-apps/applicationsets/cluster-ash-2.yaml
   ```
   The ApplicationSets will auto-deploy platform components to the new cluster.

8. The backend will automatically load the new cluster's kubeconfig from Secrets Manager on restart.

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
│  Cluster: ash-1 (with route_root_domain=true)                       │
│    Tunnel: lazycloud-prod-ash-1                                     │
│      └─ Ingress: lazycloud.dev        → nginx-ingress-controller    │
│      └─ Ingress: *.lazycloud.dev      → nginx-ingress-controller    │
│      └─ Ingress: *.ash-1.lazycloud.dev → nginx-ingress-controller   │
│    DNS: @, *, *.ash-1 → tunnel CNAME                                │
│    SSL: Advanced Certificate for *.ash-1.lazycloud.dev              │
│         (root domain uses Cloudflare Universal SSL)                 │
│                                                                     │
│  Cluster: ash-2 (when added, route_root_domain=false)               │
│    Tunnel: lazycloud-prod-ash-2                                     │
│      └─ Ingress: *.ash-2.lazycloud.dev → nginx-ingress-controller   │
│    DNS: *.ash-2 → tunnel CNAME                                      │
│    SSL: Advanced Certificate for *.ash-2.lazycloud.dev              │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  ArgoCD (Hub-Spoke Model)                                           │
├─────────────────────────────────────────────────────────────────────┤
│  ash-1 (Hub):                                                       │
│    - ArgoCD runs here (is-hub: "true")                              │
│    - Manages itself via kubernetes.default.svc                      │
│    - Manages remote clusters via their API endpoints                │
│                                                                     │
│  ash-2, fsn-1, etc. (Spokes):                                       │
│    - Registered as cluster secrets in ArgoCD                        │
│    - ApplicationSets auto-deploy based on label selectors:          │
│        provider: hetzner  → platform components                     │
│        cluster_id: ash-2  → cluster-specific values files           │
│                                                                     │
│  Cluster secrets: deploy/argocd-apps/applicationsets/cluster-*.yaml │
│  First cluster:   deploy/argocd-apps/applicationsets/local-cluster.yaml │
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

## Terraform Outputs

Useful outputs after `terraform apply`:

```bash
cd infrastructure/terraform/clusters/ash-1

# Kubernetes API endpoint
terraform output control_plane_vip

# Cloudflare tunnel ID (for clusters.yaml if needed)
terraform output cloudflare_tunnel_id

# AWS Secrets Manager ARN
terraform output cluster_secret_arn
```
