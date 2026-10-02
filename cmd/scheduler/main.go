// Command scheduler runs execution planning, placement, recovery, email
// delivery, workspace deletion and the compute fleet against PostgreSQL. Replicas are safe to
// run together: advisory locks serialize the planner and the placer, a
// replica that finds a lock held skips the pass, and email and deletion
// steps are claimed or idempotent. Every replica runs passes when a
// notification wakes it; only the elected leader also runs them on timers,
// and while nothing is live its quiet loops wait for a wake or a slow safety
// tick, so idle cost stays flat as replicas are added.
//
// With LAZYCLOUD_HEALTH_ADDR set it serves /healthz, failing when a loop
// stops finishing passes, and /readyz, true once every loop finished one. On
// SIGTERM loops stop; a pass in flight is cancelled, its transaction rolls
// back and another replica or the next start repeats it.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/AmbientWare/lazycloud/internal/callbacks"
	"github.com/AmbientWare/lazycloud/internal/compute"
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
	// attempt deadlines while work is live; safetyTick bounds it otherwise.
	tick = time.Second
	// leaderLock names the session advisory lock the leader holds.
	leaderLock = "scheduler_leader"
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
	// fleetTick paces capacity planning, launches, retirement, preemption
	// and connection steps; compute wakes cut it short.
	fleetTick = 5 * time.Second
	// reconcileTick paces comparing cloud hosts with the provider.
	reconcileTick = time.Minute
	// rollupTick paces folding container metric samples into minute
	// points; a pass that is behind reruns at once.
	rollupTick = 30 * time.Second
)

// version is stamped by the image build with -ldflags "-X main.version=...".
var version = "dev"

func main() {
	format, err := telemetry.LogFormatFromEnv(telemetry.LogJSON)
	if err != nil {
		fmt.Fprintln(os.Stderr, "scheduler:", err)
		os.Exit(2)
	}
	logger := telemetry.NewLogger(os.Stderr, format, "scheduler")
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	err = run(ctx, logger)
	stop()
	if err != nil {
		logger.Error("scheduler stopped", "error", err)
		os.Exit(1)
	}
}

func run(ctx context.Context, logger *slog.Logger) error {
	telemetryConfig, err := telemetry.ConfigFromEnv("scheduler", "")
	if err != nil {
		return err
	}
	url := os.Getenv("LAZYCLOUD_DATABASE_URL")
	if url == "" {
		return errors.New("LAZYCLOUD_DATABASE_URL is required")
	}

	pool, err := database.Open(ctx, url)
	if err != nil {
		return err
	}
	defer pool.Close()
	// LISTEN, the metering lock and the migration lock hold session state.
	session, closeSession, err := database.OpenSession(ctx, os.Getenv("LAZYCLOUD_DATABASE_SESSION_URL"), pool)
	if err != nil {
		return err
	}
	defer closeSession()
	if err := database.Migrate(ctx, session); err != nil {
		return fmt.Errorf("migrate: %w", err)
	}

	tel, err := telemetry.New(ctx, telemetryConfig)
	if err != nil {
		return err
	}
	defer func() { _ = tel.Shutdown(context.WithoutCancel(ctx)) }()
	tel.RegisterPool(pool)
	timed := newPassTimer(tel)
	p := newPace()
	beats := newHeartbeats(p.isLeading)
	// every wraps a loop's pass with its deadline, heartbeat, trace and
	// duration metric.
	every := func(name string, interval time.Duration, pass func(context.Context) bool) func(context.Context) bool {
		return beats.track(name, interval, timed(name, pass))
	}
	obs := observability.NewObservability(pool, observability.Config{}, logger)

	objectStore, err := objectStoreFromEnv()
	if err != nil {
		return err
	}
	store := storage.NewStorage(pool, objectStore)
	exec := execution.NewExecution(pool)
	sched := scheduling.NewScheduling(pool, logger)
	fleet, err := compute.LoadFleet(ctx, os.Getenv)
	if err != nil {
		return err
	}
	// Cloud instances reach the server across a network, so they always
	// dial with TLS.
	computeConfig := compute.Config{
		InstallURL: os.Getenv("LAZYCLOUD_INSTALL_URL"), ServerAddress: os.Getenv("LAZYCLOUD_AGENT_SERVER_ADDR"), Fleet: fleet,
	}
	if err := computeConfig.CheckFleet(); err != nil {
		return err
	}
	comp := compute.NewCompute(pool, exec, computeConfig)
	keyFile := os.Getenv("LAZYCLOUD_SECRETS_KEY_FILE")
	if keyFile == "" {
		return errors.New("LAZYCLOUD_SECRETS_KEY_FILE is required: callbacks are signed with workspace secrets")
	}
	masterKey, err := secrets.LoadFileKey(keyFile)
	if err != nil {
		return err
	}
	crons := schedules.NewSchedules(pool, exec)
	vault := secrets.NewSecrets(pool, masterKey)
	deliverer := callbacks.NewCallbacks(pool, vault, callbacks.CallbackConfig{
		AllowPrivateTargets: os.Getenv("LAZYCLOUD_CALLBACK_ALLOW_PRIVATE") == "1",
	}, logger)
	// Build recovery needs no registry: it only reads and moves build state.
	im := images.NewImages(pool, exec, vault, images.Config{})
	listener := database.NewListener(session, logger, database.ChannelExecution, database.ChannelImageBuild,
		notifications.Channel, identity.ChannelWorkspace, compute.ChannelCompute, schedules.Channel, database.ChannelCallback)
	fleetWake, cancelFleetWake := listener.Subscribe(compute.ChannelCompute, "")
	defer cancelFleetWake()
	capacityWake, cancelCapacityWake := listener.Subscribe(database.ChannelExecution, "")
	defer cancelCapacityWake()
	capacityFleetWake, cancelCapacityFleetWake := listener.Subscribe(compute.ChannelCompute, "")
	defer cancelCapacityFleetWake()
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
	liveWake, cancelLiveWake := listener.Subscribe(database.ChannelExecution, "")
	defer cancelLiveWake()
	schedulesWake, cancelSchedulesWake := listener.Subscribe(schedules.Channel, "")
	// Queued callbacks wake delivery at once rather than on its next tick.
	callbacksWake, cancelCallbacksWake := listener.Subscribe(database.ChannelCallback, "")
	defer cancelCallbacksWake()
	defer cancelSchedulesWake()
	// Planning signals placement after creating containers. One pending
	// value is enough: placement reads every pending container.
	placeNow := make(chan struct{}, 1)

	var healthListener net.Listener
	if addr := os.Getenv("LAZYCLOUD_HEALTH_ADDR"); addr != "" {
		var lc net.ListenConfig
		if healthListener, err = lc.Listen(ctx, "tcp", addr); err != nil {
			return fmt.Errorf("listen on %s: %w", addr, err)
		}
	}

	group, ctx := errgroup.WithContext(ctx)
	group.Go(func() error { return listener.Run(ctx) })
	leading := func(leading bool) {
		if leading {
			beats.rebase(time.Now())
			logger.InfoContext(ctx, "leading timed passes")
		}
		p.setLeading(leading)
	}
	group.Go(func() error { return database.Lead(ctx, session, leaderLock, leaderCheck, leading, logger) })
	group.Go(func() error { return p.watch(ctx, exec.HasLiveWork, liveWake, logger) })
	group.Go(func() error { return tel.ServeMetrics(ctx, logger) })
	group.Go(func() error {
		return p.loop(ctx, cadence{every: rollupTick}, nil, nil, every("metrics_rollup", rollupTick, func(ctx context.Context) bool {
			result, err := obs.RollUp(ctx)
			if err != nil {
				logger.ErrorContext(ctx, "metrics rollup pass", "error", err)
			}
			// Behind reruns the pass at once, like a contended lock.
			return result.Behind
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: tick, quiet: true}, planWake, nil, every("plan", tick, func(ctx context.Context) bool {
			result, err := exec.Plan(ctx, logger)
			if err != nil {
				logger.ErrorContext(ctx, "planning pass", "error", err)
			}
			serving, err := exec.PlanServing(ctx, logger)
			if err != nil {
				logger.ErrorContext(ctx, "serving planning pass", "error", err)
			}
			pods, err := exec.PlanPods(ctx, logger)
			if err != nil {
				logger.ErrorContext(ctx, "pod planning pass", "error", err)
			}
			result.Skipped = result.Skipped || serving.Skipped || pods.Skipped
			result.Created += serving.Created + pods.Created
			if result.Created > 0 {
				// New pending containers are live work before any report
				// wakes the probe.
				p.setLive(true)
				select {
				case placeNow <- struct{}{}:
				default:
				}
			}
			return result.Skipped
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: tick, quiet: true}, placeWake, placeNow, every("place", tick, func(ctx context.Context) bool {
			result, err := sched.Place(ctx)
			if err != nil {
				logger.ErrorContext(ctx, "placement pass", "error", err)
			}
			return result.Skipped
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: tick, quiet: true}, nil, nil, every("workloads", tick, func(ctx context.Context) bool {
			if _, err := exec.StopIdle(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "idle instance pass", "error", err)
			}
			if err := exec.TakeAutomaticSnapshots(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "snapshot pass", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: tick, quiet: true}, nil, nil, every("deadlines", tick, func(ctx context.Context) bool {
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
		return p.loop(ctx, cadence{every: tick, quiet: true, due: crons.NextFire}, schedulesWake, nil, every("schedules", tick, func(ctx context.Context) bool {
			if _, err := crons.Fire(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "schedule pass", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: tick, quiet: true, due: deliverer.NextDue}, callbacksWake, nil, every("callbacks", tick, func(ctx context.Context) bool {
			if _, err := deliverer.Deliver(ctx); err != nil {
				logger.ErrorContext(ctx, "callback pass", "error", err)
			}
			return false
		}))
	})
	// Purging finished callbacks runs apart, so a long delivery pass never
	// holds it back, and on the leader's timer like other upkeep.
	group.Go(func() error {
		return p.loop(ctx, cadence{every: purgeInterval}, nil, nil, every("callback_purge", purgeInterval, func(ctx context.Context) bool {
			if _, err := deliverer.Purge(ctx); err != nil {
				logger.ErrorContext(ctx, "callback purge", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: hostLossTick}, nil, nil, every("host_loss", hostLossTick, func(ctx context.Context) bool {
			if _, err := exec.ReleaseLostHosts(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "host loss pass", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: fleetTick}, capacityWake, capacityFleetWake, func(ctx context.Context) bool {
			result, err := comp.Plan(ctx, logger)
			if err != nil {
				logger.ErrorContext(ctx, "fleet planning pass", "error", err)
			}
			return result.Skipped
		})
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: fleetTick}, fleetWake, nil, func(ctx context.Context) bool {
			if _, err := comp.Launch(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "launch pass", "error", err)
			}
			if _, err := comp.Actuate(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "reserve actuator pass", "error", err)
			}
			if _, err := comp.Retire(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "retire pass", "error", err)
			}
			if _, err := comp.Preempt(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "preempt pass", "error", err)
			}
			if _, err := comp.AdvanceConnections(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "connection pass", "error", err)
			}
			return false
		})
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: compute.SpotPriceInterval}, nil, nil, every("spot_prices", compute.SpotPriceInterval, func(ctx context.Context) bool {
			if _, err := comp.RefreshSpotPrices(ctx, logger); err != nil && ctx.Err() == nil {
				logger.WarnContext(ctx, "spot price refresh incomplete", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: compute.QuotaInterval}, nil, nil, every("ec2_quotas", compute.QuotaInterval, func(ctx context.Context) bool {
			if _, err := comp.RefreshQuotas(ctx, logger); err != nil && ctx.Err() == nil {
				logger.WarnContext(ctx, "ec2 quota refresh incomplete", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: reconcileTick}, nil, nil, func(ctx context.Context) bool {
			if err := comp.Reconcile(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "reconcile pass", "error", err)
			}
			return false
		})
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: sweepTick}, nil, nil, every("storage_sweep", sweepTick, func(ctx context.Context) bool {
			if _, err := store.Sweep(ctx, logger); err != nil && ctx.Err() == nil {
				logger.WarnContext(ctx, "storage sweep incomplete", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: buildRecoveryTick}, buildWake, nil, every("build_recovery", buildRecoveryTick, func(ctx context.Context) bool {
			if _, err := im.Recover(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "image build recovery pass", "error", err)
			}
			return false
		}))
	})
	accounts.start(ctx, group, p, beats)
	newBillingLoops(pool, session, exec, store, logger).start(ctx, group, p)
	if healthListener != nil {
		server := &http.Server{Handler: beats.handler(), ReadHeaderTimeout: 5 * time.Second}
		group.Go(func() error {
			if err := server.Serve(healthListener); !errors.Is(err, http.ErrServerClosed) {
				return fmt.Errorf("serve health: %w", err)
			}
			return nil
		})
		group.Go(func() error {
			<-ctx.Done()
			if err := server.Close(); err != nil {
				return fmt.Errorf("close health server: %w", err)
			}
			return nil
		})
	}
	logger.Info("scheduler started", "version", version)
	if err := group.Wait(); err != nil {
		return fmt.Errorf("scheduler loops: %w", err)
	}
	logger.Info("scheduler stopped")
	return nil
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
