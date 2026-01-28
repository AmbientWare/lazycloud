# Ash-1 Cluster Configuration
# Sensitive values should be set via environment variables:
#   export TF_VAR_hcloud_token="..."
#   export TF_VAR_aws_access_key_id="..."
#   export TF_VAR_aws_secret_access_key="..."
#   export TF_VAR_juicefs_token="..."

# Cluster identity
cluster_name = "lazycloud-prod"

# Hetzner configuration
location            = "ash"
control_plane_count = 1
control_plane_type  = "cpx31"
worker_count        = 4
worker_type         = "cpx41"

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
argocd_domain            = "argocd.lazycloud.dev"
git_repo_url             = "git@github.com:AmbientWare/lazycloud.git"
argocd_repo_ssh_key_path = "~/.ssh/id_rsa"
