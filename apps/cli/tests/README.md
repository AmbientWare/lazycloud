# LazyCloud CLI Tests

This directory contains tests for the LazyCloud CLI application.

## Quick Start

```bash
# Run all unit tests
uv run pytest tests/unit/ -m unit

# Run with coverage
uv run pytest tests/unit/ --cov=cli --cov-report=html --cov-report=term -m unit

# Run E2E tests (requires backend running)
uv run pytest tests/e2e/ -m e2e
```

## Test Structure

```
tests/
├── unit/           # Fast, isolated unit tests
│   ├── conftest.py
│   ├── commands/   # Command-specific tests
│   ├── config/     # Configuration tests
│   └── ...
├── e2e/            # End-to-end integration tests
│   ├── conftest.py
│   └── ...
└── README.md       # This file
```

## Writing Tests

See [TESTS.md](../TESTS.md) for comprehensive testing guidelines.

### Key Principles

1. **Write high-value tests** - Test behavior, not implementation
2. **Avoid low-value tests** - Don't test framework features or cosmetics
3. **Use parametrized tests** - Reduce duplication
4. **Mock external dependencies** - Keep tests fast and isolated

### Example

```python
@pytest.mark.unit
def test_login_saves_api_key_to_config(mock_home_dir, sample_workspaces, mocker):
    """Test that successful login persists API key to config file"""
    # Arrange
    mock_api = mocker.patch("cli.commands.login.api")
    mock_api.workspaces.list_workspaces.return_value = sample_workspaces

    # Act
    result = runner.invoke(app, ["test_key_123"])

    # Assert
    assert result.exit_code == 0
    config_file = mock_home_dir / ".lazycloud"
    assert "API_KEY=test_key_123" in config_file.read_text()
```

## Test Coverage

Current coverage: **30.88%** overall

Modules with 100% coverage:
- `cli/config.py` (24 tests)
- `cli/commands/login.py` (10 tests)
- `cli/commands/__init__.py` (exit codes)

See [TESTS.md](../TESTS.md) for detailed coverage metrics and gaps.

## Requirements

### Unit Tests
- No external dependencies
- Runs in < 1 second
- Uses mocked filesystem and API

### E2E Tests
- Requires backend API running
- Docker Compose stack (`docker compose up -d`)
- Real CLI subprocess execution

## Contributing

1. Write tests for new features
2. Maintain 100% coverage on modified files
3. Follow test quality guidelines in [TESTS.md](../TESTS.md)
4. Run tests locally before submitting PR

## Resources

- [TESTS.md](../TESTS.md) - Comprehensive testing guide
- [pytest docs](https://docs.pytest.org/)
- [Typer testing](https://typer.tiangolo.com/tutorial/testing/)
