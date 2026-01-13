# LazyCloud

LazyCloud converts Docker Compose files into Kubernetes Helm charts, making Kubernetes accessible to developers familiar with Docker Compose.

## Project Structure

```
lazycloud/
├── apps/
│   ├── backend/          # FastAPI API server
│   ├── web/              # Next.js web frontend
│   └── cli/              # CLI tool
├── packages/             # Shared Python packages
│   ├── models/           # Shared data models
│   ├── api_requests/     # API request schemas
│   └── responses/        # API response schemas
├── deploy/               # Kubernetes deployments (GitOps)
│   ├── argocd-apps/      # ArgoCD Application manifests
│   │   ├── root-app.yaml # App of Apps entry point
│   │   └── platform/     # Platform component apps
│   ├── platform/         # Platform Helm charts (ArgoCD-managed)
│   │   ├── karpenter/
│   │   ├── external-secrets/
│   │   ├── prometheus-stack/
│   │   └── ...
│   └── services/         # Application Helm charts
│       ├── api-platform/ # API + workers deployment
│       └── web/          # Web frontend deployment
├── infrastructure/       # AWS CDK infrastructure code
├── .github/workflows/    # CI/CD pipelines
├── pyproject.toml        # Workspace root config
└── uv.lock              # Unified lockfile for all packages
```

### UV Workspace

This is a **uv workspace monorepo** with a single lockfile (`uv.lock`) for all packages:

**Workspace members:**
- `apps/backend` - API server
- `apps/cli` - CLI tool
- `packages/models` - Shared models
- `packages/api_requests` - Request schemas
- `packages/responses` - Response schemas
- `infrastructure` - AWS CDK infrastructure

**How it works:**
```bash
# Add dependency to specific member
cd apps/backend
uv add fastapi  # Adds to apps/backend only

# If member A depends on member B, B's deps are automatically available
# e.g., backend depends on packages/models, so models' deps install too

# Sync from any directory updates venv for that workspace member
cd apps/backend
uv sync  # Syncs backend + its dependencies

cd apps/cli
uv sync  # Syncs CLI + its dependencies

# Sync everything from root
uv sync --all-packages
```

**Benefits:**
- Single `uv.lock` ensures consistent versions across all packages
- Shared packages (`packages/*`) can be imported by apps
- Fast installs with shared cache

## Key Technologies

- **FastAPI** - API server
- **Next.js** - Web frontend
- **Kubernetes & Helm** - Container orchestration
- **SAQ** - Background task queue
- **PostgreSQL** - Database
- **Redis** - Cache and task queue
- **Argo CD** - GitOps deployments

## Local Development

### Prerequisites

**For local development:**
- **Python 3.13+**
- **uv** - Python package manager ([install](https://docs.astral.sh/uv/))
- **Docker & Docker Compose**
- **Node.js 22+**
- **Minikube** - For local K8s testing ([install](https://minikube.sigs.k8s.io/docs/start/))
- **kubectl** - Kubernetes CLI ([install](https://kubernetes.io/docs/tasks/tools/))
- **Helm** - K8s package manager ([install](https://helm.sh/docs/intro/install/))

**For AWS infrastructure (production):**
- **AWS CLI** - Configured with appropriate credentials ([install](https://aws.amazon.com/cli/))
- **AWS CDK** - `npm install -g aws-cdk` ([docs](https://aws.amazon.com/cdk/))

### Quick Start

```bash
# 1. Create Docker network
docker network create lazycloud

# 2. Install Python dependencies
uv sync

# 3. Start Minikube (for K8s testing)
uv run mk-up

# 4. Start backend services
docker compose up -d

# 5. Start web dev server (optional)
cd apps/web
npm install
npm run dev
```

### Access Services

- **API**: http://localhost:8000/docs
- **Web**: http://localhost:3000
- **LocalStack**: http://localhost:4566 (AWS emulation)

### Minikube Commands

```bash
# Start local K8s cluster
uv run mk-up

# Start fresh (delete existing cluster)
uv run mk-up --fresh

# Check cluster status
uv run mk-status

# Open K8s dashboard
uv run mk-dash

# Stop cluster
uv run mk-down

# Delete cluster completely
uv run mk-down --delete
```

**What Minikube provides:**
- Local Kubernetes cluster for testing deployments
- gVisor runtime for container isolation
- Depot integration for container builds
- Monitoring stack (Prometheus + Grafana)
- EFS-compatible storage class

### Development Workflow

```bash
# Backend hot reload (auto-restart on code changes)
cd apps/backend
uv run uvicorn backend.main:app --reload

# Frontend dev server (auto-refresh)
cd apps/web
npm run dev

# Run background workers
docker compose up background-workers

# Run tests
cd apps/backend && uv run pytest
cd apps/cli && uv run pytest
```

## Deployment

### Infrastructure

**2-Stack CDK + Manual ArgoCD**:

```
CDK                              Manual Install
───                              ──────────────
lazycloud-shared
  └─ IAM, Secrets

lazycloud-prod-infra             helm install argocd
  └─ VPC, EKS, Pod Identity      kubectl apply root-app.yaml
  └─ ConfigMap ────────────────→       │
                                       └─→ ArgoCD syncs everything
```

- See [infrastructure/README.md](infrastructure/README.md) for detailed setup
- **GitOps** - ArgoCD syncs from git to Kubernetes

### Deploy to Staging

```bash
# Go to GitHub Actions → Deploy Web → Run workflow
# Select "staging" → Run

# Or deploy backend
# Go to GitHub Actions → Deploy Backend → Run workflow
# Select "staging" → Run
```

### Deploy to Production

```bash
# Same as staging, but select "production"
# Production deployments require approval
```

### How It Works

1. Merge code to `main`
2. Trigger manual deployment workflow
3. Builds Docker image with SHA: `lazycloud-api:abc1234`
4. Updates Helm values file: `tag: "abc1234"`
5. Commits change to git
6. Argo CD detects change and deploys to K8s

**Same image deploys to staging and production** - no rebuilds!

### AWS Infrastructure Setup

```bash
cd infrastructure

# Deploy CDK stacks
uv run cdk deploy lazycloud-shared
uv run cdk deploy lazycloud-prod-us-east-1-infra

# Configure kubectl
aws eks update-kubeconfig --region us-east-1 --name lazycloud-prod-us-east-1-eks

# Install ArgoCD
helm repo add argo https://argoproj.github.io/argo-helm
helm install argocd argo/argo-cd -n argocd --create-namespace \
    -f argocd-values.yaml --wait --timeout 15m
kubectl apply -f ../deploy/argocd-apps/root-app.yaml
```

See [infrastructure/README.md](infrastructure/README.md) for details.

### Helm Charts

```bash
# Render staging manifests
helm template lazycloud-api-platform ./deploy/services/api-platform \
  -f ./deploy/services/api-platform/values-staging.yaml

# Render production manifests
helm template lazycloud-api-platform ./deploy/services/api-platform \
  -f ./deploy/services/api-platform/values-prod.yaml
```

## CLI Usage

```bash
# Install CLI
uv run --with lazycloud-cli lazycloud --help

# Authenticate
uv run lazycloud auth add <key-name>

# Deploy compose to K8s
cd /path/to/compose/project
uv run lazycloud init
uv run lazycloud deploy

# Dashboard
uv run lazycloud dashboard
```

## Development

### Testing

```bash
# Backend tests
cd apps/backend
uv run pytest

# CLI tests
cd apps/cli
uv run pytest
```

### Database Migrations

```bash
# Create migration (with autogenerate)
./scripts/db_revision.sh -m "description"

# Apply migrations (runs automatically with docker compose up)
# Or manually:
docker compose up db-migration

# Rollback one revision
docker compose run --rm api uv run alembic downgrade -1

# Check current version
docker compose run --rm api uv run alembic current
```

**Note:** Migrations run automatically when you `docker compose up` - the `db-migration` service ensures the database is up to date before other services start.

### Utility Scripts

```bash
# Create secret key for encryption
uv run python scripts/create_secret_key.py

# Generate admin API token
./scripts/generate_admin_token.sh

# Setup Docker network
./scripts/setup_network.sh
```

## Architecture

### Components

- **API** - FastAPI backend, REST endpoints, compose parsing
- **Web** - Next.js frontend, deployment dashboard
- **CLI** - Typer CLI with Textual TUI
- **Background Worker** - SAQ task workers (build/deploy jobs)
- **Cron Worker** - Scheduled task workers

### Deployment Flow

```
Code → GitHub → CI/CD → Depot Registry → Git (Helm values) → ArgoCD → Kubernetes
                                                              ↓
                                            deploy/argocd-apps/root-app.yaml
                                                     ↓
                                     ┌───────────────┴───────────────┐
                                     ↓                               ↓
                            deploy/platform/                 deploy/services/
                         (karpenter, prometheus...)       (api-platform, web)
```

### Environments

- **Staging**: `staging.lazycloud.dev` / `api-staging.lazycloud.dev`
- **Production**: `lazycloud.dev` / `api.lazycloud.dev`

## License

[Add your license information here]
