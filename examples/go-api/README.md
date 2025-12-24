# Go API Example

A minimal Go HTTP API ready to deploy with LazyCloud.

## Files

- `main.go` - HTTP server with hello and health endpoints
- `go.mod` - Go module definition
- `Dockerfile` - Multi-stage build for small image
- `compose.yaml` - Docker Compose with LazyCloud labels

## Deploy

```bash
cd examples/go-api
lazycloud init
lazycloud deploy
```

## Local Development

```bash
go run main.go
```

Then visit http://localhost:8080
