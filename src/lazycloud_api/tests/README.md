# LazyCloud API Tests

This directory contains the comprehensive test suite for the LazyCloud Docker Compose to Kubernetes conversion functionality.

## Test Structure

### 📋 `test_compose_parser.py`
Tests for the Docker Compose parser (`services/compose/parser.py`):
- Basic YAML parsing and validation
- Complex service configurations (ports, volumes, environment)
- Protocol validation (TCP/UDP/SCTP)
- Extension field support (x- fields)
- Error handling for invalid configurations

### ⚙️ `test_helm_values_generator.py`
Tests for the Helm values generator (`services/k8s/helm_values_generator.py`):
- Pull policy logic (latest vs version tags)
- Resource conversion (CPU/memory limits and requests)
- HPA configuration generation
- Ingress configuration with petnames
- ConfigMap generation from labels
- Metrics/Prometheus annotations
- Restart policy mapping
- Port protocol parsing and validation
- Volume management

### 🔗 `test_integration.py`
End-to-end integration tests:
- Complete pipeline from Docker Compose to Helm values
- Comprehensive service configuration testing
- Volume management across services
- Validation warnings generation
- YAML serialization compatibility

## Running Tests

### Basic Test Run
```bash
uv run pytest src/lazycloud_api/tests/
```

### With Coverage
```bash
uv run pytest src/lazycloud_api/tests/ --cov=src/lazycloud_api/services --cov-report=term-missing
```

### Specific Test Categories
```bash
# Run only parser tests
uv run pytest src/lazycloud_api/tests/test_compose_parser.py

# Run only generator tests  
uv run pytest src/lazycloud_api/tests/test_helm_values_generator.py

# Run only integration tests
uv run pytest src/lazycloud_api/tests/test_integration.py
```

### Verbose Output
```bash
uv run pytest src/lazycloud_api/tests/ -v
```

## Test Coverage

Current coverage: **80%** across the compose and k8s services.

Coverage includes:
- ✅ All major functionality paths
- ✅ Error conditions and edge cases
- ✅ Complex configuration scenarios
- ✅ Integration between components

## Test Fixtures

The `conftest.py` file provides shared fixtures:
- `compose_parser`: ComposeParser instance
- `helm_generator`: HelmValuesGenerator instance  
- `sample_compose_data`: Comprehensive test data
- `test_compose_file`: Temporary compose file for file-based tests

## Features Tested

### Docker Compose Parsing ✅
- Service definitions with all supported fields
- Port mappings with protocols
- Volume mounts (named volumes, bind mounts)
- Environment variables (dict and list formats)
- Health checks
- Deploy configurations (resources, replicas, restart policies)
- Networks and dependencies
- Extension fields (x- prefixed)

### Kubernetes Generation ✅
- **Workload Types**: Deployment generation
- **Resources**: CPU/memory limits and requests conversion
- **Scaling**: HPA with CPU/memory targets and scaling policies
- **Networking**: Ingress with hostname prefixes and TLS
- **Storage**: PVC generation from named volumes
- **Configuration**: ConfigMaps from file labels
- **Monitoring**: Prometheus metrics annotations
- **Security**: Protocol validation, restart policies
- **Pull Policies**: Smart defaults based on image tags

### Production Features ✅
- Auto-generated petnames for ingress
- Resource unit conversion (G→Gi, M→Mi)
- Port range and IP binding support
- Validation warnings for unsupported features
- YAML serialization compatibility

## Adding New Tests

When adding new functionality:

1. **Unit Tests**: Add specific tests in the appropriate test file
2. **Integration Tests**: Add end-to-end tests in `test_integration.py`
3. **Fixtures**: Add reusable test data to `conftest.py`
4. **Coverage**: Ensure new code paths are tested

Example test structure:
```python
def test_new_feature(self, helm_generator, compose_parser):
    """Test description."""
    # Arrange
    compose_dict = {...}
    
    # Act
    compose_file = compose_parser.parse_dict(compose_dict)
    helm_values = helm_generator.generate_values(compose_file)
    
    # Assert
    assert helm_values["expected_field"] == "expected_value"
```