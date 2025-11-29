# Release Directory

This directory contains all files related to building and releasing the LazyCloud CLI binaries.

## Files

- **build.spec** - PyInstaller configuration for building standalone binaries
- **release_readme.md** - README file that gets copied to the `lazycloud-releases` repository

## Release Process

### 1. Update Version

Update the version in [apps/cli/pyproject.toml](../pyproject.toml):

```toml
[project]
name = "lazycloud"
version = "0.0.2"  # Update this
```

### 2. Create a Git Tag

```bash
git tag v0.0.2
git push origin v0.0.2
```

### 3. GitHub Actions Workflow

The [release workflow](../../../.github/workflows/release.yml) will automatically:

1. Build binaries for all platforms (Linux, macOS Intel/ARM, Windows)
2. Create tarballs/zips with SHA256 checksums
3. Checkout the `AmbientWare/lazycloud-releases` repository
4. Copy `release_readme.md` as the main README
5. Copy all binaries to a versioned directory
6. Commit and push to the releases repo
7. Create a GitHub release with all artifacts

### 4. Required Secret

Add a fine-grained Personal Access Token as a GitHub secret:

**Secret Name:** `RELEASES_REPO_TOKEN`

**Permissions:**
- Repository: `AmbientWare/lazycloud-releases`
- Permissions: Contents (Read and Write)

To create:
1. Go to GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens
2. Create new token
3. Select only `AmbientWare/lazycloud-releases` repository
4. Grant Contents: Read and write
5. Add as secret in main repo: Settings → Secrets and variables → Actions → New repository secret

## Release Repository Structure

After release, the `lazycloud-releases` repository will look like:

```
lazycloud-releases/
├── README.md (copied from release_readme.md)
├── v0.0.1/
│   ├── lazycloud-linux-amd64.tar.gz
│   ├── lazycloud-linux-amd64.tar.gz.sha256
│   ├── lazycloud-darwin-amd64.tar.gz
│   ├── lazycloud-darwin-amd64.tar.gz.sha256
│   ├── lazycloud-darwin-arm64.tar.gz
│   ├── lazycloud-darwin-arm64.tar.gz.sha256
│   ├── lazycloud-windows-amd64.zip
│   └── lazycloud-windows-amd64.zip.sha256
└── v0.0.2/
    └── ...
```

## Local Testing

To test the build locally:

```bash
cd apps/cli
uv sync
uv pip install pyinstaller
uv run pyinstaller release/build.spec --clean
./dist/lazycloud --version
```

## Installation Scripts

The installation scripts download from the public `lazycloud-releases` repository:

- **Linux/macOS:** `curl -LsSf https://lazycloud.dev/install.sh | sh`
- **Windows:** `irm https://lazycloud.dev/install.ps1 | iex`

These scripts download from:
- `https://github.com/AmbientWare/lazycloud-releases/releases/latest/download/lazycloud-{platform}-{arch}.tar.gz`
