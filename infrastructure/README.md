# Lazycloud Infrastructure - AWS CDK

Modern Infrastructure as Code using AWS CDK and Python for the lazycloud platform.

## 🏗️ Architecture

This infrastructure includes:

- **Shared Infrastructure**: DNS/Route53 hosted zone, SSL certificates (deployed once, shared across environments)
- **Environment Components**: VPC, EKS Cluster, S3 buckets, ElastiCache, ArgoCD

## 📋 Prerequisites

1. **Python 3.13+** with `uv` package manager
2. **AWS CLI** configured with appropriate credentials
3. **AWS CDK** installed globally: `npm install -g aws-cdk`
4. **kubectl** for Kubernetes managementinfras

## 🚀 Quick Start

### 1. Install Dependencies

```bash
uv sync
```

### 2. Deploy Infrastructure

**First Time CDK Initialization**
```bash
# only run if repo has not been initialized
cdk init
```

```bash
# List available stacks
cdk list

# Deploy shared infrastructure first (DNS, SSL certificates)
cdk deploy lazycloud-shared

# Deploy dev environment
cdk deploy lazycloud-dev

# Deploy production environment
cdk deploy lazycloud-prod

# Deploy all stacks
cdk deploy --all
```

### 3. Configure kubectl

```bash
# For dev:
aws eks update-kubeconfig --region us-east-1 --name lazycloud-dev-eks

# For production:
aws eks update-kubeconfig --region us-east-1 --name lazycloud-prod-eks
```

### 4. Access ArgoCD

```bash
# Port-forward to ArgoCD UI
kubectl port-forward -n argocd svc/argocd-server 8080:80

# Get admin password
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d

# Access UI at: http://localhost:8080
# Username: admin
```

## 📁 Project Structure

```
infrastructure/
├── app.py                      # CDK app entry point
├── cdk.json                    # CDK configuration
├── infrastructure/
│   ├── config/
│   │   └── environments.py     # Environment configurations
│   ├── stacks/
│   │   ├── dev/
│   │   │   └── stack.py        # Dev stack orchestration
│   │   ├── prod/
│   │   │   └── stack.py        # Production stack orchestration
│   │   └── shared/
│   │       ├── stack.py        # Shared stack (DNS, SSL)
│   │       └── shared.py       # DNS/Route53 infrastructure
│   └── constructs/
│       ├── components/         # Consolidated components
│       │   ├── shared_infrastructure.py    # VPC + EKS
│       │   ├── storage_services.py         # S3 + ElastiCache
│       │   └── platform_services.py        # ArgoCD + Helm
│       ├── aws/                # Low-level AWS constructs
│       ├── helm/               # Helm chart constructs
│       └── apps/               # Application constructs
```

## 🔧 Configuration

Environment settings are in `infrastructure/config/environments.py`:

- VPC CIDR blocks and subnets
- EKS cluster settings
- ElastiCache capacity limits
- ArgoCD configuration

Shared DNS configuration is in `infrastructure/stacks/shared/shared.py`:

- Domain: lazycloud.com
- Wildcard SSL certificate: *.lazyclous.dev
- DNS records (currently commented out - configure as needed)

## 🎯 Environment Differences

| Component | Production | Development |
|-----------|------------|-------------|
| VPC CIDR | 10.0.0.0/16 | 10.2.0.0/16 |
| NAT Gateways | Multi-AZ (HA) | Single (cost optimized) |
| ElastiCache | 50GB, 20K ECPU | 10GB, 5K ECPU |

## 🛠️ Development Workflow

```bash
# Check changes before deploy
cdk diff lazycloud-dev

# Synthesize CloudFormation templates
cdk synth

# Format code
ruff format .

# Deploy specific environment
cdk deploy lazycloud-dev
```

## 🔐 Security Features

- VPC with private subnets for workloads
- EKS and private API endpoint
- S3 encryption at rest, block public access
- ElastiCache VPC-only access
- IAM least privilege access

## 🗑️ Cleanup

```bash
# Destroy specific environment
cdk destroy lazycloud-dev
cdk destroy lazycloud-prod
```

## 💰 Cost Estimates

- **Development**: ~$120-150/month
- **Production**: ~$200-400/month

## 🚨 Troubleshooting

1. **EKS issues**: Check AWS service limits and IAM permissions
2. **ArgoCD access**: Use `kubectl port-forward` if LoadBalancer fails
3. **ElastiCache**: Verify security group rules and VPC connectivity

For detailed debugging: `CDK_DEBUG=true cdk deploy`
