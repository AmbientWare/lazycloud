variable "service_name" {
  type        = string
  description = "Service name used for generated infrastructure names."
  default     = "lazycloud"
}

variable "region" {
  type        = string
  description = "AWS region for the infrastructure."
  default     = "us-east-1"
}

variable "availability_zones" {
  type        = list(string)
  description = "Availability zones used by public and private subnets."
  default     = ["us-east-1a", "us-east-1b"]
}

variable "vpc_cidr" {
  type        = string
  description = "VPC CIDR block."
  default     = "10.42.0.0/16"
}

variable "public_subnet_cidrs" {
  type        = list(string)
  description = "Public subnet CIDRs."
  default     = ["10.42.0.0/24", "10.42.1.0/24"]
}

variable "private_subnet_cidrs" {
  type        = list(string)
  description = "Private subnet CIDRs."
  default     = ["10.42.10.0/24", "10.42.11.0/24"]
}

variable "allowed_ingress_cidrs" {
  type        = list(string)
  description = "CIDR ranges allowed to reach the control-plane API and Kubernetes API."
  default     = ["0.0.0.0/0"]
}

variable "ssh_ingress_cidrs" {
  type        = list(string)
  description = "CIDR ranges allowed to SSH to the control-plane host for chart installation."
  default     = ["0.0.0.0/0"]
}

variable "control_plane_port" {
  type        = number
  description = "Control-plane HTTP port."
  default     = 9000
}

variable "control_plane_public_http_url" {
  type        = string
  description = "Externally reachable control-plane HTTP origin installed into the Helm chart."
  default     = ""

  validation {
    condition = (
      var.control_plane_public_http_url == ""
      || can(regex("^https?://[^[:space:]]+$", var.control_plane_public_http_url))
    )
    error_message = "control_plane_public_http_url must be empty or an absolute HTTP(S) URL."
  }
}

variable "node_ami" {
  type        = string
  description = "AMI used for K3s control-plane and worker nodes."
}

variable "control_plane_instance_type" {
  type        = string
  description = "EC2 instance type for the K3s control-plane node."
  default     = "t3.large"
}

variable "worker_instance_type" {
  type        = string
  description = "EC2 instance type for K3s worker nodes."
  default     = "t3.large"
}

variable "worker_count" {
  type        = number
  description = "Number of K3s worker nodes."
  default     = 1
}

variable "object_bucket_name" {
  type        = string
  description = "S3 bucket used for user objects."
}

variable "image_bucket_name" {
  type        = string
  description = "S3 bucket used for image/build artifacts."
}

variable "postgres_version" {
  type        = string
  description = "PostgreSQL engine version."
  default     = "16"
}

variable "database_instance_class" {
  type        = string
  description = "RDS instance class."
  default     = "db.t4g.micro"
}

variable "database_storage_gb" {
  type        = number
  description = "Allocated RDS storage in GiB."
  default     = 20
}

variable "database_name" {
  type        = string
  description = "Application database name."
  default     = "lazycloud"
}

variable "database_username" {
  type        = string
  description = "Application database username."
  default     = "lazycloud"
}

variable "skip_final_snapshot" {
  type        = bool
  description = "Whether RDS should skip a final snapshot on destroy."
  default     = false
}

variable "install_chart" {
  type        = bool
  description = "Upload the local Helm chart to the K3s control-plane and install chart resources."
  default     = false
}

variable "ssh_user" {
  type        = string
  description = "SSH user used by Terraform to apply chart resources."
  default     = "ubuntu"
}

variable "ssh_key_name" {
  type        = string
  description = "Existing EC2 key pair attached to K3s nodes for SSH access."
  default     = null
}

variable "ssh_private_key_path" {
  type        = string
  description = "Private key path used by Terraform when install_chart is true."
  default     = ""
  sensitive   = true
}

variable "api_image" {
  type        = string
  description = "API image repository installed by Terraform."
  default     = "api"
}

variable "scheduler_image" {
  type        = string
  description = "Scheduler image repository installed by Terraform."
  default     = "scheduler"
}

variable "container_worker_image" {
  type        = string
  description = "Container-worker image repository installed by Terraform."
  default     = "container-worker"
}

variable "database_bootstrap_image" {
  type        = string
  description = "Database bootstrap image repository installed by Terraform."
  default     = "database-bootstrap"
}

variable "storage_gateway_image" {
  type        = string
  description = "Storage-gateway image repository installed by Terraform."
  default     = "storage-gateway"
}

variable "worker_bootstrap_image" {
  type        = string
  description = "Worker token bootstrap image repository installed by Terraform."
  default     = "worker-bootstrap"
}

variable "cache_image" {
  type        = string
  description = "Cache server image repository installed by Terraform."
  default     = "cache-server"
}

variable "image_tag" {
  type        = string
  description = "Image tag installed by Terraform."
  default     = "latest"
}

variable "image_pull_policy" {
  type        = string
  description = "Image pull policy installed by Terraform."
  default     = "IfNotPresent"
}

variable "hostname" {
  type        = string
  description = "Hostname rendered into ingress when ingress is enabled."
  default     = "lazycloud.example.com"
}

variable "ingress_enabled" {
  type        = bool
  description = "Enable chart ingress when Terraform applies chart resources."
  default     = false
}

variable "monitoring_enabled" {
  type        = bool
  description = "Enable chart monitoring resources when Terraform applies chart resources."
  default     = true
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to AWS resources."
  default     = {}
}
