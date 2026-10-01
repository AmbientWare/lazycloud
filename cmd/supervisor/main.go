// Command supervisor is PID 1 in every workload container. The agent mounts
// it read-only and makes it the entrypoint. Build it static with
// CGO_ENABLED=0 so it runs in any image.
//
// `supervisor netfilter '<policy json>'` applies a container's egress
// policy; the agent runs it in a helper container that joins the
// container's network namespace.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/AmbientWare/lazycloud/internal/supervisor"
)

// Exit codes the agent can observe with docker inspect. A pod's supervisor
// exits with its command's code.
const (
	exitFailed     = 1
	exitUsage      = 2
	exitLoadFailed = 3
)

func main() {
	os.Exit(run())
}

func run() int {
	if len(os.Args) > 1 && os.Args[1] == "netfilter" {
		if len(os.Args) != 3 {
			fmt.Fprintln(os.Stderr, `usage: supervisor netfilter '{"block":bool,"allow":["cidr",...]}'`)
			return exitUsage
		}
		if err := supervisor.ApplyNetworkPolicy(os.Args[2]); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return exitFailed
		}
		return 0
	}
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil)).With("component", "supervisor")
	socket := os.Getenv(supervisor.SocketEnv)
	if socket == "" {
		logger.Error("missing agent socket", "env", supervisor.SocketEnv)
		return exitUsage
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	err := supervisor.Run(ctx, supervisor.Config{
		Socket: socket, APISocket: os.Getenv(supervisor.APISocketEnv), Reap: os.Getpid() == 1, Logger: logger,
	})
	var exited *supervisor.CommandExitedError
	switch {
	case err == nil:
		return 0
	case errors.As(err, &exited):
		return exited.Code
	case errors.Is(err, supervisor.ErrLoadFailed):
		return exitLoadFailed
	case ctx.Err() != nil:
		return 128 + int(syscall.SIGTERM)
	default:
		logger.Error("supervisor failed", "error", err)
		return exitFailed
	}
}
