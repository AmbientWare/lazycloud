# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LazyCloud converts Docker Compose files to Kubernetes Helm charts, making Kubernetes accessible to Docker Compose users. It includes an API server, CLI tool, and async workers.

## Common Commands

### Development Setup
```bash
# Create Docker network (first time only)
docker network create lazycloud

# Copy environment file
cp .env.example .env

# Install dependencies
uv sync

# Start all services
docker compose up -d

# Run API server with hot reload
uv run python src/lazycloud_api/main.py --debug

# Run CLI
uv run lazycloud
```

### Testing
```bash
# Run all tests with coverage
./scripts/test.sh

# Run specific test file
./scripts/test.sh src/lazycloud_api/tests/test_parser.py

# Run with verbose output
./scripts/test.sh -v

# Run parser tests only
./scripts/test.sh -k parser

# Run generator tests only
./scripts/test.sh -k generator
```

### Database Operations
```bash
# Create new migration
./scripts/db_revision.sh -m "description_of_changes"

# Apply migrations
alembic upgrade head

# Check migration status
alembic current
```

### Minikube/Kubernetes Testing
```bash
# Start minikube
minikube-up

# Open dashboard
minikube-dash

# Stop minikube
minikube-down
```

## Architecture

### Directory Structure
- `src/lazycloud_api/` - Backend API service
  - `api/v1/` - API endpoints
  - `services/compose/` - Docker Compose parsing logic
  - `services/k8s/` - Kubernetes/Helm generation logic
  - `celery_app/` - Async task workers
  - `database/` - SQLAlchemy models and CRUD
- `src/lazycloud_cli/` - CLI application
- `src/shared/` - Shared models between API and CLI
- `compose_tests/` - Test Docker Compose files

### Key Components

1. **Compose Parser** (`services/compose/parser.py`)
   - Validates Docker Compose syntax
   - Enforces Kubernetes naming conventions early
   - Extracts service configurations

2. **Helm Values Generator** (`services/k8s/helm_values_generator.py`)
   - Converts parsed compose to Helm values
   - Handles resource limits, environment variables, volumes
   - Generates init containers for dependencies

3. **K8s Configuration** (`services/k8s/configuration.py`)
   - Manages Kubernetes client configuration
   - Handles multiple cluster contexts

4. **API Endpoints** (`api/v1/compose.py`)
   - POST /compose/deploy - Deploy compose file
   - DELETE /compose/destroy - Remove deployment
   - GET /compose/status - Check deployment status

### Important Design Decisions

1. **Strict Validation**: Names must be Kubernetes-compliant from the start (lowercase, alphanumeric, hyphens only)

2. **Resource Defaults**: If limits are specified without requests, requests are auto-generated at 80% of limits

3. **Security Contexts**: Automatically determined based on image (e.g., nginx runs as uid 101)

4. **Network Isolation**: Uses NetworkPolicy for inter-service communication

5. **Init Containers**: Dependencies are handled via init containers that wait for services to be ready

## Scaling Configuration

LazyCloud supports auto-scaling through labels:
```yaml
labels:
  lazycloud.scaling.enabled: "true"
  lazycloud.scaling.min: "1"       # Minimum replicas
  lazycloud.scaling.max: "10"      # Maximum replicas  
  lazycloud.scaling.cpu: "70"      # CPU utilization target (%)
  lazycloud.scaling.memory: "80"   # Memory utilization target (%)
```

When scaling is enabled, the service will automatically scale between min and max replicas based on CPU/memory usage.

## Known Limitations

See `compose_tests/CHALLENGES.md` for detailed list. Key items:
- No support for: build contexts, profiles, static IPs, privileged mode
- Partial support for: health checks, complex networks, some volume types
- Different behavior for: restart policies (Kubernetes handles differently)

## Testing Guidelines

- Minimum 80% test coverage required
- Parser tests: Test individual compose file parsing
- Generator tests: Test Helm values generation
- Integration tests: Test full deployment flow
- Use pytest fixtures for common test data

## Debugging Tips

1. **API Debugging**: Run with `--debug` flag for auto-reload
2. **Database Issues**: Check migrations with `alembic current`
3. **Kubernetes Issues**: Use `minikube-dash` to inspect deployments
4. **Parser Errors**: Validation errors include specific field paths
5. **Generator Issues**: Generated Helm values are logged at debug level

## Common Tasks

### Adding New Compose Feature Support
1. Update parser models in `services/compose/models.py`
2. Add parsing logic in `services/compose/parser.py`
3. Add generation logic in `services/k8s/helm_values_generator.py`
4. Add tests in `tests/services/compose/` and `tests/services/k8s/`
5. Update `compose_tests/CHALLENGES.md` if limitations change

### Modifying API Endpoints
1. Update endpoint in `api/v1/`
2. Update corresponding CLI command in `src/lazycloud_cli/commands/`
3. Update API client in `src/lazycloud_cli/api/`
4. Add/update tests

### Database Schema Changes
1. Modify models in `database/models.py`
2. Create migration: `./scripts/db_revision.sh -m "description"`
3. Review generated migration in `migrations/versions/`
4. Apply: `alembic upgrade head`