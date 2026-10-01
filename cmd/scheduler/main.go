// Command scheduler runs execution planning, placement, recovery, email
// delivery and workspace deletion against PostgreSQL. Replicas are safe to
// run together: advisory locks serialize the planner and the placer, a
// replica that finds a lock held skips the pass, and email and deletion
// steps are claimed or idempotent.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/notifications"
	"github.com/AmbientWare/lazycloud/internal/scheduling"
)

const (
	// tick bounds how long a missed wake delays planning, placement and
	// attempt deadlines.
	tick = time.Second
	// hostLossTick paces host-loss detection; hosts are lost after
	// compute.LivenessTimeout, so detection lags by at most this much.
	hostLossTick = 5 * time.Second
	// contendedRetry reruns a pass that found its lock held, because the
	// holder may have read state before this replica's change committed.
	contendedRetry = 100 * time.Millisecond
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := run(logger); err != nil {
		logger.Error("scheduler stopped", "error", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	url := os.Getenv("LAZYCLOUD_DATABASE_URL")
	if url == "" {
		return errors.New("LAZYCLOUD_DATABASE_URL is required")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	pool, err := database.Open(ctx, url)
	if err != nil {
		return err
	}
	defer pool.Close()
	if err := database.Migrate(ctx, pool); err != nil {
		return fmt.Errorf("migrate: %w", err)
	}

	exec := execution.NewExecution(pool)
	sched := scheduling.NewScheduling(pool, logger)
	listener := database.NewListener(pool, logger, database.ChannelExecution, notifications.Channel, identity.ChannelWorkspace)
	accounts, cancelAccounts, err := newAccountLoops(pool, exec, listener, logger)
	if err != nil {
		return err
	}
	defer cancelAccounts()
	planWake, cancelPlanWake := listener.Subscribe(database.ChannelExecution, "")
	defer cancelPlanWake()
	placeWake, cancelPlaceWake := listener.Subscribe(database.ChannelExecution, "")
	defer cancelPlaceWake()
	// Planning signals placement after creating containers. One pending
	// value is enough: placement reads every pending container.
	placeNow := make(chan struct{}, 1)

	group, ctx := errgroup.WithContext(ctx)
	group.Go(func() error { return listener.Run(ctx) })
	group.Go(func() error {
		return loop(ctx, tick, planWake, nil, func(ctx context.Context) bool {
			result, err := exec.Plan(ctx, logger)
			if err != nil {
				logger.ErrorContext(ctx, "planning pass", "error", err)
			}
			if result.Created > 0 {
				select {
				case placeNow <- struct{}{}:
				default:
				}
			}
			return result.Skipped
		})
	})
	group.Go(func() error {
		return loop(ctx, tick, placeWake, placeNow, func(ctx context.Context) bool {
			result, err := sched.Place(ctx)
			if err != nil {
				logger.ErrorContext(ctx, "placement pass", "error", err)
			}
			return result.Skipped
		})
	})
	group.Go(func() error {
		return loop(ctx, tick, nil, nil, func(ctx context.Context) bool {
			if _, err := exec.TimeOutAttempts(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "attempt deadline pass", "error", err)
			}
			if _, err := exec.TimeOutStarts(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "start deadline pass", "error", err)
			}
			return false
		})
	})
	group.Go(func() error {
		return loop(ctx, hostLossTick, nil, nil, func(ctx context.Context) bool {
			if _, err := exec.ReleaseLostHosts(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "host loss pass", "error", err)
			}
			return false
		})
	})
	accounts.start(ctx, group)
	logger.Info("scheduler started")
	if err := group.Wait(); err != nil {
		return fmt.Errorf("scheduler loops: %w", err)
	}
	logger.Info("scheduler stopped")
	return nil
}

// loop runs pass now, then after every wake, tick or contended retry until
// ctx ends. pass reports whether it found its lock held. Failures are logged
// by pass and retried on the next wake or tick; a nil wake channel is never
// ready.
func loop(ctx context.Context, interval time.Duration, wake, alsoWake <-chan struct{}, pass func(context.Context) bool) error {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		var retry <-chan time.Time
		if pass(ctx) {
			retry = time.After(contendedRetry)
		}
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		case <-wake:
		case <-alsoWake:
		case <-retry:
		}
	}
}
