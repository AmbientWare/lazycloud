# LazyCloud CLI Testing Documentation

## Overview

This document tracks testing strategy, coverage, and gaps for the LazyCloud CLI application.

## Current Test Structure

```
apps/cli/tests/
├── e2e/                    # End-to-end integration tests
│   ├── conftest.py         # Test fixtures and helpers
│   ├── test_cli_basics.py  # Help/version commands
│   ├── test_cli_deployments.py
│   ├── test_cli_workspaces.py
│   ├── test_cli_rollback.py
│   └── test_api_endpoints.py
└── unit/                   # Unit tests
    ├── conftest.py         # Mock fixtures
    ├── commands/
    │   └── test_login.py   # Login command tests
    ├── config/
    │   └── test_config.py  # Config file handling tests
    └── test_exit_codes.py  # Exit code consistency tests
```

## Test Coverage Status

### Commands Coverage

| Command | E2E Tests | Unit Tests | Status |
|---------|-----------|------------|--------|
| `login` | ❌ | ✅ | **Unit Only** |
| `init` | ✅ | ❌ | E2E Only |
| `deploy` | ✅ | ❌ | E2E Only |
| `destroy` | ✅ | ❌ | E2E Only |
| `rollback` | ✅ | ❌ | E2E Only |
| `dashboard` | ❌ | ❌ | **UNTESTED** |
| `usage` | ❌ | ❌ | **UNTESTED** |
| `workspaces list` | ✅ | ❌ | E2E Only |
| `workspaces create` | ✅ | ❌ | E2E Only |
| `workspaces activate` | ✅ | ❌ | E2E Only |
| `workspaces destroy` | ✅ | ❌ | E2E Only |
| `deployments list` | ❌ | ❌ | **UNTESTED** |
| `--version` | ❌ | ✅ | **Unit Only** |

### Feature Coverage

#### ✅ Well-Covered
- **Login command** (12 unit tests, 100% coverage)
- **Config file handling** (24 unit tests, 100% coverage)
- **Exit code consistency** (19 unit tests)
- **Version command** (implemented and tested)
- Help/usage output for all commands
- Init → Deploy → Destroy lifecycle
- Workspace CRUD operations
- Invalid compose file rejection
- Multi-service deployments
- Image build support
- Kubernetes service verification
- Rollback with revision history
- Idempotent resource operations

#### ⚠️ Partially Covered
- Error handling (only basic cases)
- Build workflows (only happy path)
- Deployment modifications

#### ❌ Not Covered
- Interactive TUI (dashboard)
- Usage/billing information
- Deployment listing
- Environment variable expansion
- Secrets management
- Volume handling
- Network configuration
- Service dependencies
- API error responses (unit level)
- Network timeouts/failures (unit level)
- Confirmation prompts

## Testing Strategy

### Test Layers

#### 1. Unit Tests (TODO)
**Purpose:** Fast, isolated testing of individual components
**Dependencies:** None (mocked)
**Execution Time:** < 1 second per test
**Coverage Target:** 80%+

**Priority Areas:**
- Config file parsing/validation
- API client methods
- Compose file validation
- Command argument parsing
- Output formatters
- Utility functions

#### 2. Integration Tests (TODO)
**Purpose:** Test component interactions without full stack
**Dependencies:** Mocked API, filesystem
**Execution Time:** < 5 seconds per test
**Coverage Target:** Key workflows

**Priority Areas:**
- Config + API client interaction
- Command → API → response flow
- File operations + validation

#### 3. E2E Tests (Existing)
**Purpose:** Full workflow validation
**Dependencies:** Backend API, Docker, Kubernetes
**Execution Time:** Varies (30s - 2min per test)
**Coverage Target:** Critical user paths

**Current Coverage:**
- 31 E2E tests across 5 test files
- All major workflows tested
- Real subprocess CLI execution

## Test Gaps & Priorities

### Phase 1: Critical Gaps ✅ COMPLETED

#### 1.1 Login Command Testing ✅
**Priority:** CRITICAL
**Type:** Unit
**Status:** COMPLETE - 10 unit tests, 100% coverage

- [x] API key validation
- [x] Credential storage
- [x] Workspace detection
- [x] Invalid key rejection
- [x] Authentication state verification
- [x] Config file creation
- [x] Interactive mode
- [x] Network error handling
- [x] Whitespace stripping
- [x] Missing workspace scenarios

#### 1.2 Exit Code Consistency ✅
**Priority:** HIGH
**Type:** Unit
**Status:** COMPLETE - 5 tests

- [x] Version command returns 0
- [x] Invalid command returns non-zero
- [x] Login errors return 1
- [x] Unauthenticated commands return 1

#### 1.3 Unit Test Infrastructure ✅
**Priority:** HIGH
**Type:** Infrastructure
**Status:** COMPLETE

- [x] Create `tests/unit/` directory structure
- [x] Add pytest-mock dependency
- [x] Add pytest-cov for coverage reporting
- [x] Add pytest-timeout for safety
- [x] Create mock fixtures for API responses
- [x] Document unit test patterns
- [x] Configure coverage reporting (HTML + terminal)

### Phase 2: Important Gaps

#### 2.1 Dashboard Command
**Priority:** MEDIUM
**Type:** Unit

- [ ] TUI initialization
- [ ] State management
- [ ] Error handling
- [ ] Data refresh logic

#### 2.2 Usage Command
**Priority:** MEDIUM
**Type:** Unit + E2E

- [ ] Data retrieval
- [ ] Formatting/display
- [ ] Error handling

#### 2.3 Deployments List
**Priority:** MEDIUM
**Type:** E2E

- [ ] List all deployments
- [ ] Filtering logic
- [ ] State display
- [ ] Empty state handling

#### 2.4 Error Scenarios
**Priority:** MEDIUM
**Type:** Unit + Integration

- [ ] Network timeouts
- [ ] API error responses (4xx, 5xx)
- [ ] Malformed responses
- [ ] Partial deployment failures
- [ ] Authentication expiry

#### 2.5 Config File Handling ✅
**Priority:** MEDIUM
**Type:** Unit
**Status:** COMPLETE - 24 tests, 100% coverage

- [x] Parse valid config
- [x] Invalid format rejection
- [x] Missing file handling
- [x] Workspace switching
- [x] API key storage/retrieval
- [x] Environment variable fallbacks
- [x] Priority handling (file vs env)
- [x] Authentication validation

### Phase 3: Nice-to-Have

#### 3.1 Docker Compose Features
- [ ] Environment variable expansion
- [ ] Secrets management
- [ ] Volume handling
- [ ] Network configuration
- [ ] Service dependencies

#### 3.2 Interactive Elements
- [ ] Confirmation prompt testing
- [ ] Progress indicators
- [ ] Color output
- [ ] Terminal width handling

#### 3.3 Advanced Scenarios
- [ ] Update existing deployments
- [ ] Scale operations
- [ ] Rolling updates
- [ ] Build error handling
- [ ] Registry authentication

## Testing Tools & Dependencies

### Current Dependencies
```toml
[tool.uv.dev-dependencies]
pytest = "^9.0.1"
pytest-asyncio = "^1.3.0"
```

### Recommended Additions
```toml
pytest-mock = "^3.14.0"      # Mocking support
pytest-cov = "^6.0.0"        # Coverage reporting
pytest-xdist = "^3.6.0"      # Parallel execution
pytest-timeout = "^2.3.0"    # Timeout enforcement
```

## Running Tests

### All Tests
```bash
cd apps/cli
uv run pytest tests/
```

### E2E Tests Only
```bash
uv run pytest tests/e2e/ -m e2e
```

### Unit Tests Only (TODO)
```bash
uv run pytest tests/unit/
```

### With Coverage (TODO)
```bash
uv run pytest --cov=cli --cov-report=html --cov-report=term
```

### Specific Test File
```bash
uv run pytest tests/e2e/test_cli_workspaces.py
```

## Test Requirements

### E2E Tests
- Docker Compose stack running (`docker compose up -d`)
- Backend API available at http://localhost:3000
- Database initialized and accessible
- Valid API key in environment

### Unit Tests
- No external dependencies
- Fast execution (< 1 second)
- Isolated from filesystem/network (use mocks)
- 100% coverage on tested modules

## Test Quality Guidelines

### What Makes a Good Test?

**High-Value Tests:**
- Test actual behavior and business logic
- Verify error handling and edge cases
- Test critical paths (authentication, data loss scenarios)
- Validate state changes (config file updates, workspace switching)
- Check integration points between modules

**Low-Value Tests to Avoid:**
- Testing framework behavior (e.g., `--help` flags provided by Typer)
- Asserting output formatting details (specific text colors, exact wording)
- Testing library functionality (e.g., that httpx raises connection errors)
- Duplicate tests with minor variations
- Tests that only assert a function was called without behavior validation

**Examples:**

✅ Good: `test_login_saves_api_key_to_config_file` - Tests actual state change
❌ Bad: `test_login_displays_success_message` - Tests UI formatting details

✅ Good: `test_config_prefers_file_over_env_var` - Tests important precedence logic
❌ Bad: `test_deploy_help_returns_zero` - Tests framework functionality

✅ Good: `test_login_clears_key_on_failure` - Tests error recovery behavior
❌ Bad: `test_login_shows_green_checkmark` - Tests cosmetic output

### Best Practices

#### Do's
✅ Use descriptive test names (`test_login_with_invalid_api_key_clears_stored_credentials`)
✅ Use parametrized tests for multiple similar scenarios
✅ Isolate tests with fixtures (mock home directory, API clients)
✅ Test both success and failure paths
✅ Use temporary directories for file operations
✅ Mock external dependencies in unit tests
✅ Verify exit codes for all command paths
✅ Test state changes, not just return values
✅ Keep tests focused - one behavior per test
✅ Write tests that would catch real bugs

#### Don'ts
❌ Test built-in framework behavior (help flags, version flags)
❌ Assert on cosmetic output details (colors, specific wording)
❌ Write redundant tests that test the same logic
❌ Share state between tests
❌ Skip cleanup on failure
❌ Use hardcoded paths
❌ Rely on test execution order
❌ Mock what you don't own (in integration tests)
❌ Test implementation details instead of behavior

## Coverage Metrics

### Target Coverage
- Unit Tests: 80%+ line coverage for tested modules
- Integration Tests: Key workflows covered (TODO)
- E2E Tests: Critical user paths covered

### Current Coverage (as of 2025-11-28)
- **Overall:** 30.88% line coverage (6463 total lines)
- **Unit Tests:** 39 tests covering critical modules
- **E2E Tests:** 31 tests covering major workflows
- **Modules with 100% Coverage:**
  - [cli/config.py](src/cli/config.py) - Configuration handling (86 lines)
  - [cli/commands/login.py](src/cli/commands/login.py) - Login command (50 lines)
  - [cli/commands/__init__.py](src/cli/commands/__init__.py) - CLI entry point (27 lines)
  - [cli/ui/colors.py](src/cli/ui/colors.py) - Color theme (41 lines)
  - [cli/ui/textual/theme.py](src/cli/ui/textual/theme.py) - TUI theme (36 lines)
  - [cli/api/__init__.py](src/cli/api/__init__.py) - API client (33 lines)
  - [cli/api/versions.py](src/cli/api/versions.py) - Version API (9 lines)

## CI/CD Integration

### Current State
- Backend tests run in GitHub Actions
- CLI tests NOT in CI pipeline

### TODO
- [ ] Add CLI test job to `.github/workflows/`
- [ ] Run unit tests on every PR
- [ ] Run E2E tests on main branch
- [ ] Generate coverage reports
- [ ] Add coverage badges to README
- [ ] Set minimum coverage thresholds

## Contributing

### Adding New Tests

1. Determine test type (unit/integration/e2e)
2. Create test file in appropriate directory
3. Use existing fixtures from `conftest.py`
4. Follow naming convention: `test_<feature>_<scenario>.py`
5. Update this document with coverage status
6. Ensure tests pass locally before PR

### Test File Organization

```python
# Good test structure
def test_command_success_scenario():
    """Test description"""
    # Arrange
    setup_test_data()

    # Act
    result = run_command()

    # Assert
    assert result.exit_code == 0
    assert "expected output" in result.stdout


def test_command_error_scenario():
    """Test error handling"""
    # Arrange, Act, Assert
    ...
```

## Resources

- [pytest documentation](https://docs.pytest.org/)
- [Typer testing guide](https://typer.tiangolo.com/tutorial/testing/)
- [Textual testing guide](https://textual.textualize.io/guide/testing/) (for TUI)

## Changelog

### 2025-11-28
- ✅ Created TESTS.md documentation
- ✅ Added unit test infrastructure (pytest-mock, pytest-cov, pytest-timeout)
- ✅ Implemented 39 high-quality unit tests
- ✅ Achieved 100% coverage on config.py (24 tests)
- ✅ Achieved 100% coverage on login command (10 tests)
- ✅ Added exit code consistency tests (5 tests)
- ✅ Configured coverage reporting (HTML + terminal)
- ✅ Added test quality guidelines
- ✅ Implemented --version command
- ✅ Overall codebase coverage: 30.88%
