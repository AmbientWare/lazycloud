# LazyCloud CLI

LazyCloud is a CLI tool for deploying Docker Compose applications to the cloud with zero configuration.

## Installation

### Linux and macOS

```bash
curl -LsSf https://lazycloud.dev/install.sh | sh
```

### Windows (PowerShell)

```powershell
irm https://lazycloud.dev/install.ps1 | iex
```

## Quick Start

After installation:

```bash
# Login to LazyCloud
lazycloud login

# Initialize a new deployment
lazycloud init

# Deploy your application
lazycloud deploy

# View your deployments
lazycloud deployments list
```

## Features

- 🚀 Deploy Docker Compose apps with a single command
- 🔄 Automatic rollback support
- 📊 Built-in resource monitoring
- 🌐 Custom domain support
- 💰 Usage tracking and billing

## Available Commands

- `lazycloud login` - Authenticate with your API key
- `lazycloud init` - Initialize a deployment configuration
- `lazycloud deploy` - Deploy or update your application
- `lazycloud destroy` - Remove a deployment
- `lazycloud rollback` - Rollback to a previous version
- `lazycloud deployments` - Manage your deployments
- `lazycloud workspaces` - Manage workspaces
- `lazycloud usage` - View usage metrics
- `lazycloud dashboard` - Launch the interactive dashboard
- `lazycloud --version` - Show CLI version

## Documentation

Visit [lazycloud.dev](https://lazycloud.dev) for full documentation.

## Support

- Issues: [github.com/AmbientWare/lazycloud/issues](https://github.com/AmbientWare/lazycloud/issues)
- Email: support@lazycloud.dev

## License

Copyright © 2025 AmbientWare
