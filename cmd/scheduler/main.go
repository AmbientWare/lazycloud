// Command scheduler runs execution planning, placement and recovery against
// PostgreSQL. Replicas are safe to run together: advisory locks serialize the
// planner and the placer, and a replica that finds a lock held skips the pass.
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
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/notifications"
	"github.com/AmbientWare/lazycloud/internal/schedules"
	"github.com/AmbientWare/lazycloud/internal/scheduling"
	"github.com/AmbientWare/lazycloud/internal/secrets"
)

const (
	// tick bounds how long a missed wake delays planning, placement and
	// attempt deadlines.
	tick = time.Second
	// hostLossTick paces host-loss detection; hosts are lost after
	// compute.LivenessTimeout, so detection lags by at most this much.
	hostLossTick = 5 * time.Second
	// buildRecoveryTick paces build deadlines; container stops wake recovery
	// at once.
	buildRecoveryTick = 10 * time.Second
	// contendedRetry reruns a pass that found its lock held, because the
	// holder may have read state before this replica's change committed.
	contendedRetry = 100 * time.Millisecond
	// purgeInterval paces deletion of finished callbacks.
	purgeInterval = time.Minute
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
	keyFile := os.Getenv("LAZYCLOUD_SECRETS_KEY_FILE")
	if keyFile == "" {
		return errors.New("LAZYCLOUD_SECRETS_KEY_FILE is required: callbacks are signed with workspace secrets")
	}
	masterKey, err := secrets.LoadFileKey(keyFile)
	if err != nil {
		return err
	}
	crons := schedules.NewSchedules(pool, exec)
	callbacks := notifications.NewCallbacks(pool, secrets.NewSecrets(pool, masterKey), notifications.CallbackConfig{
		AllowPrivateTargets: os.Getenv("LAZYCLOUD_CALLBACK_ALLOW_PRIVATE") == "1",
	}, logger)
	// Build recovery needs no registry: it only reads and moves build state.
	im := images.NewImages(pool, exec, images.Config{}, nil)
	listener := database.NewListener(pool, logger, database.ChannelExecution, database.ChannelImageBuild)
	buildWake, cancelBuildWake := listener.Subscribe(database.ChannelImageBuild, "")
	defer cancelBuildWake()
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
		return loop(ctx, tick, nil, nil, func(ctx context.Context) bool {
			if _, err := crons.Fire(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "schedule pass", "error", err)
			}
			return false
		})
	})
	group.Go(func() error {
		lastPurge := time.Now()
		return loop(ctx, tick, nil, nil, func(ctx context.Context) bool {
			if _, err := callbacks.Deliver(ctx); err != nil {
				logger.ErrorContext(ctx, "callback pass", "error", err)
			}
			if time.Since(lastPurge) > purgeInterval {
				lastPurge = time.Now()
				if _, err := callbacks.Purge(ctx); err != nil {
					logger.ErrorContext(ctx, "callback purge", "error", err)
				}
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
	group.Go(func() error {
		return loop(ctx, buildRecoveryTick, buildWake, nil, func(ctx context.Context) bool {
			if _, err := im.Recover(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "image build recovery pass", "error", err)
			}
			return false
		})
	})
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
