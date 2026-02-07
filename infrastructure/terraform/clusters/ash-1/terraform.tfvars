# ash-1 Cluster Configuration
# Sensitive values should be set via environment variables:
#   export TF_VAR_hcloud_token="..."
#   export TF_VAR_aws_access_key_id="..."
#   export TF_VAR_aws_secret_access_key="..."
#   export TF_VAR_juicefs_token="..."
#   export TF_VAR_cloudflare_api_token="..."

# Cluster identity
cluster_name = "lazycloud-prod"

# Hetzner configuration
location            = "ash"
control_plane_count = 1
control_plane_type  = "cpx31"
platform_count      = 2
platform_type       = "cpx31"
worker_count        = 2
worker_type         = "ccx33"

# Kubernetes/Talos versions
kubernetes_version = "1.32.0"
talos_version      = "v1.12.2"

# Networking (unique per cluster to avoid conflicts)
network_ipv4_cidr = "10.0.0.0/16"
node_ipv4_cidr    = "10.0.1.0/24"
pod_ipv4_cidr     = "10.244.0.0/16"
service_ipv4_cidr = "10.96.0.0/12"

# JuiceFS
juicefs_name = "lazycloud-prod"

# ArgoCD
argocd_domain            = "argocd.ash-1.lazycloud.dev"
git_repo_url             = "git@github.com:AmbientWare/lazycloud.git"
argocd_repo_ssh_key_path = "~/.ssh/id_rsa"

# Cloudflare (get these from Cloudflare dashboard)
# Account ID: https://dash.cloudflare.com/ → select account → Account ID in sidebar
# Zone ID: https://dash.cloudflare.com/ → select zone → API section → Zone ID
cloudflare_account_id = "9c3af886ee7a3e3df120e61daea2ee1c"
cloudflare_zone_id    = "7de4ad3cd44f8592d1c47db937c59f4f"
cloudflare_zone       = "lazycloud.dev"

# Route root domain (lazycloud.dev and *.lazycloud.dev) through this cluster
# Only enable on one cluster - this is the primary cluster for the main app
route_root_domain = true
