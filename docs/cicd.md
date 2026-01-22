# CI/CD Integration

Automate LazyCloud deployments in your CI/CD pipelines.

## Prerequisites

**First deployment must be done locally:**

```bash
lazycloud login
lazycloud init
lazycloud deploy
```

This creates the `.lazycloud` config and stores initial secrets. After that, automate with CI/CD.

## Setup

### 1. Get Credentials

1. Go to https://lazycloud.dev
2. Click your user icon in the sidebar → **API Key**
3. Copy your API key
4. Get your workspace ID: `lazycloud workspaces list`

### 2. Add Secrets to CI/CD

| Secret | Description |
|--------|-------------|
| `LAZYCLOUD_API_KEY` | Your API key |
| `LAZYCLOUD_WORKSPACE_ID` | Target workspace ID |

Plus any app-specific secrets (DATABASE_URL, etc.)

## Deploy Command

```bash
lazycloud deploy --env shell --build-arg shell -y
```

This reads environment variables and build args from the shell and skips confirmation prompts.

## GitHub Actions

### Basic Deploy

```yaml
name: Deploy

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: curl -LsSf https://lazycloud.dev/install.sh | sh
      - run: lazycloud deploy --env shell --build-arg shell -y
        env:
          LAZYCLOUD_API_KEY: ${{ secrets.LAZYCLOUD_API_KEY }}
          LAZYCLOUD_WORKSPACE_ID: ${{ secrets.LAZYCLOUD_WORKSPACE_ID }}
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
```

### Multi-Environment (Staging → Production)

```yaml
name: Deploy

on:
  push:
    branches: [main]
  release:
    types: [published]

jobs:
  staging:
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: curl -LsSf https://lazycloud.dev/install.sh | sh
      - run: lazycloud deploy --env shell --build-arg shell -y
        env:
          LAZYCLOUD_API_KEY: ${{ secrets.LAZYCLOUD_API_KEY }}
          LAZYCLOUD_WORKSPACE_ID: ${{ secrets.WORKSPACE_ID_STAGING }}
          DATABASE_URL: ${{ secrets.DATABASE_URL_STAGING }}

  production:
    if: github.event_name == 'release'
    runs-on: ubuntu-latest
    environment: production  # Requires approval
    steps:
      - uses: actions/checkout@v4
      - run: curl -LsSf https://lazycloud.dev/install.sh | sh
      - run: lazycloud deploy --env shell --build-arg shell -y
        env:
          LAZYCLOUD_API_KEY: ${{ secrets.LAZYCLOUD_API_KEY }}
          LAZYCLOUD_WORKSPACE_ID: ${{ secrets.WORKSPACE_ID_PROD }}
          DATABASE_URL: ${{ secrets.DATABASE_URL_PROD }}
```

## GitLab CI

```yaml
deploy:
  stage: deploy
  image: ubuntu:latest
  before_script:
    - curl -LsSf https://lazycloud.dev/install.sh | sh
  script:
    - lazycloud deploy --env shell --build-arg shell -y
  variables:
    LAZYCLOUD_API_KEY: $LAZYCLOUD_API_KEY
    LAZYCLOUD_WORKSPACE_ID: $LAZYCLOUD_WORKSPACE_ID
  only:
    - main
```

## Environments

Use **separate workspaces** for each environment. Workspaces are fully isolated - separate deployments, secrets, and resources.

### Setup

1. Create workspaces in dashboard: `my-app-staging`, `my-app-production`
2. Get IDs: `lazycloud workspaces list`
3. Store in CI/CD secrets:
   - `WORKSPACE_ID_STAGING`
   - `WORKSPACE_ID_PROD`

### Local Switching

```bash
lazycloud workspaces list
lazycloud workspaces activate <workspace-name>
lazycloud deploy
```

### Best Practices

- Same `.lazycloud` file for all environments
- Environment config lives in CI/CD secrets, not code
- Deploy to staging first, then promote to production
- Use approval gates for production

## Troubleshooting

| Error | Solution |
|-------|----------|
| "No active workspace ID found" | Set `LAZYCLOUD_WORKSPACE_ID` |
| "Authentication failed" | Check `LAZYCLOUD_API_KEY` |
| "Missing environment variables" | Add missing vars to CI/CD secrets |
| "Deployment not found" | Run first deploy locally |

## Internal: CLI Release

For LazyCloud maintainers - the CLI release workflow:

1. Go to Actions → `cli_release.yml` → Run workflow
2. Select bump type:
   - `patch`: 0.1.2 → 0.1.3
   - `minor`: 0.1.2 → 0.2.0
   - `major`: 0.1.2 → 1.0.0
   - `sync-only`: Just sync examples/README
3. Workflow bumps version, builds binaries, creates release
