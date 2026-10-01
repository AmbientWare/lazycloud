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

	"github.com/AmbientWare/lazycloud/internal/callbacks"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/notifications"
	"github.com/AmbientWare/lazycloud/internal/observability"
	"github.com/AmbientWare/lazycloud/internal/schedules"
	"github.com/AmbientWare/lazycloud/internal/scheduling"
	"github.com/AmbientWare/lazycloud/internal/secrets"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
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
	// sweepTick paces storage retention: expired artifacts and map keys,
	// deleted volumes and disks, expired host keys and volume sizes.
	sweepTick = 30 * time.Second
	// purgeInterval paces deletion of finished callbacks.
	purgeInterval = time.Minute
	// rollupTick paces folding container metric samples into minute
	// points; a pass that is behind reruns at once.
	rollupTick = 30 * time.Second
)

func main() {
	format, err := telemetry.LogFormatFromEnv(telemetry.LogJSON)
	if err != nil {
		fmt.Fprintln(os.Stderr, "scheduler:", err)
		os.Exit(2)
	}
	logger := telemetry.NewLogger(os.Stderr, format, "scheduler")
	if err := run(logger); err != nil {
		logger.Error("scheduler stopped", "error", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	telemetryConfig, err := telemetry.ConfigFromEnv("scheduler", "")
	if err != nil {
		return err
	}
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

	tel, err := telemetry.New(ctx, telemetryConfig)
	if err != nil {
		return err
	}
	defer func() { _ = tel.Shutdown(context.WithoutCancel(ctx)) }()
	tel.RegisterPool(pool)
	timed := newPassTimer(tel)
	obs := observability.NewObservability(pool, observability.Config{}, logger)

	objectStore, err := objectStoreFromEnv()
	if err != nil {
		return err
	}
	store := storage.NewStorage(pool, objectStore)
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
	deliverer := callbacks.NewCallbacks(pool, secrets.NewSecrets(pool, masterKey), callbacks.CallbackConfig{
		AllowPrivateTargets: os.Getenv("LAZYCLOUD_CALLBACK_ALLOW_PRIVATE") == "1",
	}, logger)
	// Build recovery needs no registry: it only reads and moves build state.
	im := images.NewImages(pool, exec, images.Config{})
	listener := database.NewListener(pool, logger, database.ChannelExecution, database.ChannelImageBuild,
		notifications.Channel, identity.ChannelWorkspace)
	buildWake, cancelBuildWake := listener.Subscribe(database.ChannelImageBuild, "")
	defer cancelBuildWake()
	accounts, cancelAccounts, err := newAccountLoops(pool, exec, im, listener, logger)
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
	group.Go(func() error { return tel.ServeMetrics(ctx, logger) })
	group.Go(func() error {
		return loop(ctx, rollupTick, nil, nil, timed("metrics_rollup", func(ctx context.Context) bool {
			result, err := obs.RollUp(ctx)
			if err != nil {
				logger.ErrorContext(ctx, "metrics rollup pass", "error", err)
			}
			// Behind reruns the pass at once, like a contended lock.
			return result.Behind
		}))
	})
	group.Go(func() error {
		return loop(ctx, tick, planWake, nil, timed("plan", func(ctx context.Context) bool {
			result, err := exec.Plan(ctx, logger)
			if err != nil {
				logger.ErrorContext(ctx, "planning pass", "error", err)
			}
			serving, err := exec.PlanServing(ctx, logger)
			if err != nil {
				logger.ErrorContext(ctx, "serving planning pass", "error", err)
			}
			result.Skipped = result.Skipped || serving.Skipped
			result.Created += serving.Created
			if result.Created > 0 {
				select {
				case placeNow <- struct{}{}:
				default:
				}
			}
			return result.Skipped
		}))
	})
	group.Go(func() error {
		return loop(ctx, tick, placeWake, placeNow, timed("place", func(ctx context.Context) bool {
			result, err := sched.Place(ctx)
			if err != nil {
				logger.ErrorContext(ctx, "placement pass", "error", err)
			}
			return result.Skipped
		}))
	})
	group.Go(func() error {
		return loop(ctx, tick, nil, nil, timed("deadlines", func(ctx context.Context) bool {
			if _, err := exec.TimeOutAttempts(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "attempt deadline pass", "error", err)
			}
			if _, err := exec.TimeOutStarts(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "start deadline pass", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return loop(ctx, tick, nil, nil, timed("schedules", func(ctx context.Context) bool {
			if _, err := crons.Fire(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "schedule pass", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		lastPurge := time.Now()
		return loop(ctx, tick, nil, nil, timed("callbacks", func(ctx context.Context) bool {
			if _, err := deliverer.Deliver(ctx); err != nil {
				logger.ErrorContext(ctx, "callback pass", "error", err)
			}
			if time.Since(lastPurge) > purgeInterval {
				lastPurge = time.Now()
				if _, err := deliverer.Purge(ctx); err != nil {
					logger.ErrorContext(ctx, "callback purge", "error", err)
				}
			}
			return false
		}))
	})
	group.Go(func() error {
		return loop(ctx, hostLossTick, nil, nil, timed("host_loss", func(ctx context.Context) bool {
			if _, err := exec.ReleaseLostHosts(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "host loss pass", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		store.RunSweeper(ctx, sweepTick, logger)
		return nil
	})
	group.Go(func() error {
		return loop(ctx, buildRecoveryTick, buildWake, nil, timed("build_recovery", func(ctx context.Context) bool {
			if _, err := im.Recover(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "image build recovery pass", "error", err)
			}
			return false
		}))
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

// objectStoreFromEnv reads the object store the server uses; the sweeper
// deletes bytes there.
func objectStoreFromEnv() (storage.Config, error) {
	cfg := storage.Config{
		Endpoint:        os.Getenv("LAZYCLOUD_OBJECT_STORE_ENDPOINT"),
		Region:          os.Getenv("LAZYCLOUD_OBJECT_STORE_REGION"),
		Bucket:          os.Getenv("LAZYCLOUD_OBJECT_STORE_BUCKET"),
		AccessKeyID:     os.Getenv("LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID"),
		SecretAccessKey: os.Getenv("LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY"),
		Workspaces: storage.WorkspaceBuckets{
			Provider:         storage.BucketProvider(os.Getenv("LAZYCLOUD_WORKSPACE_BUCKET_PROVIDER")),
			Prefix:           os.Getenv("LAZYCLOUD_WORKSPACE_BUCKET_PREFIX"),
			GarageAdminURL:   os.Getenv("LAZYCLOUD_GARAGE_ADMIN_URL"),
			GarageAdminToken: os.Getenv("LAZYCLOUD_GARAGE_ADMIN_TOKEN"),
			RoleARN:          os.Getenv("LAZYCLOUD_WORKSPACE_BUCKET_ROLE_ARN"),
		},
	}
	if err := cfg.Validate(); err != nil {
		return cfg, fmt.Errorf("object store (LAZYCLOUD_OBJECT_STORE_*): %w", err)
	}
	if cfg.Workspaces.Prefix == "" {
		cfg.Workspaces.Prefix = "lazycloud-ws"
	}
	return cfg, nil
}
