# LazyCloud

LazyCloud converts Docker Compose files into Kubernetes Helm charts, making Kubernetes accessible to developers familiar with Docker Compose. Deploy your existing compose applications to Kubernetes without learning complex K8s configurations.

## What is LazyCloud?

LazyCloud bridges the gap between Docker Compose and Kubernetes by:
- **Parsing** Docker Compose files and validating them for Kubernetes compatibility
- **Converting** compose configurations to Helm charts with best practices
- **Deploying** applications to Kubernetes clusters with a simple CLI
- **Managing** deployments through an interactive TUI dashboard

## Key Technologies

- **FastAPI** - High-performance API server
- **Textual** - Modern terminal user interface for the CLI dashboard
- **Kubernetes & Helm** - Container orchestration and package management
- **LocalStack** - AWS ECR emulation for development
- **Prefect** - Workflow orchestration for async tasks
- **PostgreSQL** - Application database
- **Redis** - Caching and task queue

## Project Structure

```
lazycloud/
├── src/
│   ├── lazycloud_api/      # FastAPI backend service
│   │   ├── api/v1/          # REST API endpoints
│   │   ├── services/        # Business logic (compose parsing, k8s generation)
│   │   ├── database/        # SQLAlchemy models and CRUD operations
│   │   └── prefect_app/     # Async workflow tasks
│   ├── lazycloud_cli/       # CLI tool with TUI dashboard
│   │   ├── commands/        # Typer CLI commands
│   │   ├── ui/              # Textual dashboard components
│   │   └── api/             # API client for backend communication
│   ├── shared/              # Shared models between API and CLI
│   └── scripts/             # Development utilities
├── docker-compose.yml       # Development environment
└── pyproject.toml          # Project dependencies and configuration
```

## Prerequisites

Before you begin, ensure you have the following installed:

- **Python 3.13+** - Required for the application
- **uv** - Fast Python package manager ([installation](https://docs.astral.sh/uv/))
- **Docker & Docker Compose** - For running services
- **Minikube** - Local Kubernetes cluster ([installation](https://minikube.sigs.k8s.io/docs/start/))
- **kubectl** - Kubernetes CLI ([installation](https://kubernetes.io/docs/tasks/tools/))
- **Helm** - Kubernetes package manager ([installation](https://helm.sh/docs/intro/install/))

## Getting Started

Follow these steps to set up your LazyCloud development environment:

### Step 1: Create Docker Network

LazyCloud services communicate over a dedicated Docker network:

```bash
docker network create lazycloud
```

### Step 2: Install Dependencies

Install Python dependencies using uv:

```bash
uv sync
```

### Step 3: Start Minikube Development Environment

Start a local Kubernetes cluster with all required configurations:

```bash
# Start minikube with LazyCloud configuration
uv run mk-up

# Check cluster status
uv run mk-status

# Optional: Open Kubernetes dashboard
uv run mk-dash
```

**What `mk-up` does:**
- Starts Minikube with 4GB RAM, 2 CPUs, and 20GB disk
- Connects to the `lazycloud` Docker network
- Enables addons: ingress, storage-provisioner, metrics-server, gvisor
- Creates an EFS-compatible StorageClass for persistent volumes
- Sets up the `lazycloud-test` namespace
- Configures DNS for LocalStack registry access (ECR emulation)

### Step 4: Start Application Services

Launch all LazyCloud services using Docker Compose:

```bash
# Start all services in detached mode
docker compose up -d

# Or rebuild and start services
docker compose up --build -d
```

**Services started:**
- **API Server** - Backend API (port 8000)
- **PostgreSQL** - Application database (port 5432)
- **Redis** - Cache and task queue (port 6379)
- **Prefect Server** - Workflow orchestration (port 4200)
- **LocalStack** - AWS service emulation including ECR (port 4566)

### Step 5: Run Database Migrations

Initialize the database schema:

```bash
uv run alembic upgrade head
```

### Step 6: Verify Setup

Check that everything is running correctly:

- **API Documentation**: http://localhost:8000/docs
- **API Health**: http://localhost:8000/health
- **Prefect UI**: http://localhost:4200

## CLI Usage

LazyCloud provides a powerful CLI for managing your deployments.

### Authentication

Before deploying, add an API key to authenticate with the LazyCloud API:

```bash
# Add a new API key
uv run lazycloud auth add <key-name>

# List all configured API keys
uv run lazycloud auth list

# Set the active API key
uv run lazycloud auth set <key-name>

# Remove an API key
uv run lazycloud auth remove <key-name>
```

**Note:** The CLI accepts a `--server-url` flag to specify a custom API endpoint:
```bash
uv run lazycloud --server-url http://api.example.com deploy
```

### Deployment Workflow

Deploy Docker Compose applications to Kubernetes in three simple steps:

```bash
# 1. Navigate to your compose project directory
cd /path/to/your/compose/project

# 2. Initialize LazyCloud deployment configuration
uv run lazycloud init

# 3. Deploy to Kubernetes
uv run lazycloud deploy
```

### Interactive Dashboard

Launch the TUI dashboard to monitor and manage deployments:

```bash
uv run lazycloud dashboard
```

The dashboard provides:
- Real-time deployment status
- Service health monitoring
- Log streaming
- Interactive deployment management

### Destroy Deployment

Remove a deployment from Kubernetes:

```bash
# Destroy the current deployment (from project directory)
uv run lazycloud destroy

# Destroy a specific deployment by name
uv run lazycloud destroy <deployment-name>

# Skip confirmation prompt
uv run lazycloud destroy --force
```

## Development Commands

### API Development

Run the API server with hot reload for development:

```bash
uv run python src/lazycloud_api/main.py --debug
```

The API will reload automatically when you make changes to the source code.

### Minikube Management

Convenient commands for managing your local Kubernetes cluster:

```bash
# Show available commands
uv run mk-help

# Start minikube
uv run mk-up

# Delete existing cluster and start fresh
uv run mk-up --fresh

# Stop minikube (preserves cluster state)
uv run mk-down

# Delete minikube cluster completely
uv run mk-down --delete

# Show cluster status and resources
uv run mk-status

# Open Kubernetes dashboard in browser
uv run mk-dash
```

### Database Operations

Manage database schema changes with Alembic migrations:

```bash
# Create a new migration
./scripts/db_revision.sh -m "description_of_changes"

# Apply all pending migrations
uv run alembic upgrade head

# Check current migration version
uv run alembic current

# Rollback one migration
uv run alembic downgrade -1
```

### Testing

Run tests with coverage:

```bash
# Run all tests
uv run pytest

# Run with coverage report
uv run pytest --cov=src --cov-report=html

# Run specific test file
uv run pytest src/lazycloud_api/tests/test_compose_parser.py

# Run tests matching a pattern
uv run pytest -k "test_parser"
```

## API Documentation

The LazyCloud API provides comprehensive endpoints for managing deployments.

### Interactive Documentation

- **Swagger UI**: http://localhost:8000/docs - Interactive API testing interface
- **ReDoc**: http://localhost:8000/redoc - Clean, readable API documentation

### Key Endpoints

- **Deployments** - Create, update, list, and delete deployments
- **Services** - Query service status and configurations
- **Secrets** - Manage Kubernetes secrets securely
- **Registry** - Container registry authentication and management
- **Tasks** - Monitor async deployment tasks
- **Diff** - Preview changes before deployment

## Architecture Overview

### How It Works

1. **Compose Parsing** - Your Docker Compose file is validated and parsed by the API
2. **Helm Generation** - Compose configurations are converted to Kubernetes Helm charts
3. **Validation** - Resources are validated for Kubernetes compatibility
4. **Deployment** - Helm charts are deployed to your configured Kubernetes cluster
5. **Monitoring** - Prefect workflows monitor deployment progress and health

### Key Features

- **gVisor Security** - Enhanced container isolation using gVisor runtime class
- **LocalStack Integration** - Emulates AWS ECR for local development without AWS credentials
- **Automatic Dependencies** - Init containers ensure services start in the correct order
- **Resource Management** - Automatic resource request/limit configuration
- **Network Policies** - Secure inter-service communication
- **Persistent Volumes** - Automatic PVC creation for compose volumes

### Design Decisions

- **Strict Validation** - Service names must be Kubernetes-compliant (lowercase, alphanumeric, hyphens)
- **Smart Defaults** - If limits are specified without requests, requests default to 80% of limits
- **Security First** - Non-root security contexts automatically applied where possible
- **Async Tasks** - Long-running operations (deployments, monitoring) use Prefect workflows

## Troubleshooting

### Common Issues

**Minikube won't start:**
- Ensure Docker is running
- Check if the `lazycloud` network exists: `docker network ls`
- Try starting fresh: `uv run mk-up --fresh`

**API returns 500 errors:**
- Check database is running: `docker compose ps`
- Verify migrations are applied: `uv run alembic current`
- Check API logs: `docker compose logs api`

**Deployment fails:**
- Verify Minikube is running: `uv run mk-status`
- Check kubectl can connect: `kubectl get nodes`
- Ensure your compose file is valid: Check API validation errors

**LocalStack registry issues:**
- Restart LocalStack: `docker compose restart localstack`
- Reconfigure DNS: `uv run mk-up --fresh`

## Contributing

Contributions are welcome! Please see `CLAUDE.md` for development guidelines and architecture details.

## License

[Add your license information here]
