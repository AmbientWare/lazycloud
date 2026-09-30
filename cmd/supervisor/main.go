// Command supervisor is PID 1 in every workload container. The agent mounts
// it read-only and makes it the entrypoint. Build it static with
// CGO_ENABLED=0 so it runs in any image.
package main

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/AmbientWare/lazycloud/internal/supervisor"
)

// Exit codes the agent can observe with docker inspect.
const (
	exitFailed     = 1
	exitUsage      = 2
	exitLoadFailed = 3
)

func main() {
	os.Exit(run())
}

func run() int {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil)).With("component", "supervisor")
	socket := os.Getenv(supervisor.SocketEnv)
	if socket == "" {
		logger.Error("missing agent socket", "env", supervisor.SocketEnv)
		return exitUsage
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	err := supervisor.Run(ctx, supervisor.Config{Socket: socket, Reap: os.Getpid() == 1, Logger: logger})
	switch {
	case err == nil:
		return 0
	case errors.Is(err, supervisor.ErrLoadFailed):
		return exitLoadFailed
	case ctx.Err() != nil:
		return 128 + int(syscall.SIGTERM)
	default:
		logger.Error("supervisor failed", "error", err)
		return exitFailed
	}
}
