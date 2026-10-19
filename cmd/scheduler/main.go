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
	"cmp"
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
	"github.com/AmbientWare/lazycloud/internal/cpu"
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

// leaderLock names the session advisory lock the leader holds.
const leaderLock = "scheduler_leader"

// ticks paces the scheduler's loops: each is the longest its loop waits
// between passes on the leader without a wake. main runs production's.
type ticks struct {
	// tick bounds how long a missed wake delays planning, placement and
	// attempt deadlines while work is live; safety bounds it otherwise.
	tick time.Duration
	// safety bounds how long a quiet loop on the leader waits without a
	// wake while no work is live, covering a lost notification.
	safety time.Duration
	// leaderCheck paces the leader's ping of the session that holds its
	// lock, and a standby's tries for the lock.
	leaderCheck time.Duration
	// contendedRetry reruns a pass that found its lock held, because the
	// holder may have read state before this replica's change committed.
	contendedRetry time.Duration
	// hostLoss paces host-loss detection; hosts are lost after
	// compute.LivenessTimeout, so detection lags by at most this much.
	hostLoss time.Duration
	// buildRecovery paces build deadlines; container stops wake recovery
	// at once.
	buildRecovery time.Duration
	// sweep paces storage retention: expired artifacts and map keys,
	// deleted volumes and disks, expired host keys and volume sizes.
	sweep time.Duration
	// layerSweep paces the sweep of converted image layers; each pass
	// reads every live image reference, and pairs wait a day anyway.
	layerSweep time.Duration
	// purge paces deletion of finished callbacks.
	purge time.Duration
	// fleet paces capacity planning, launches, retirement and preemption;
	// compute wakes cut it short.
	fleet time.Duration
	// connections paces connection steps, so a setup check comes due
	// within this of its time.
	connections time.Duration
	// networks paces the fleet network check.
	networks time.Duration
	// quotas paces the check for EC2 quotas due a read, so a newly
	// connected account's are read within it of its networks.
	quotas time.Duration
	// reconcile paces comparing cloud hosts with the provider.
	reconcile time.Duration
	// rollup paces folding container metric samples into minute
	// points; a pass that is behind reruns at once.
	rollup time.Duration
	// email bounds how long a missed wake or a due retry waits.
	email time.Duration
	// housekeeping paces pruning expired sessions and device codes and
	// purging old email bodies.
	housekeeping time.Duration
	// deletion paces workspace deletion while hosts stop containers.
	deletion time.Duration
	// metering paces metering, balance rollups and enforcement, so a
	// balance trails usage by about this much.
	metering time.Duration
	// payments paces Stripe deliveries, purchases and plan changes.
	payments time.Duration
	// retention paces unfunded data retention, which is counted in days.
	retention time.Duration
}

func productionTicks() ticks {
	return ticks{
		tick: time.Second, safety: 30 * time.Second, leaderCheck: 10 * time.Second, contendedRetry: 100 * time.Millisecond,
		hostLoss: 5 * time.Second, buildRecovery: 10 * time.Second, sweep: 30 * time.Second, layerSweep: 10 * time.Minute,
		purge: time.Minute, fleet: 5 * time.Second, connections: time.Second, networks: 15 * time.Second, quotas: 15 * time.Second, reconcile: time.Minute, rollup: 30 * time.Second,
		email: 5 * time.Second, housekeeping: time.Minute, deletion: 2 * time.Second,
		metering: 15 * time.Second, payments: 5 * time.Second, retention: time.Minute,
	}
}

func (t ticks) validate() error {
	for _, tick := range []struct {
		name string
		d    time.Duration
	}{
		{"tick", t.tick}, {"safety", t.safety}, {"leader check", t.leaderCheck}, {"contended retry", t.contendedRetry},
		{"host loss", t.hostLoss}, {"build recovery", t.buildRecovery}, {"sweep", t.sweep}, {"layer sweep", t.layerSweep},
		{"purge", t.purge}, {"fleet", t.fleet}, {"connections", t.connections}, {"networks", t.networks}, {"quotas", t.quotas}, {"reconcile", t.reconcile}, {"rollup", t.rollup},
		{"email", t.email}, {"housekeeping", t.housekeeping}, {"deletion", t.deletion},
		{"metering", t.metering}, {"payments", t.payments}, {"retention", t.retention},
	} {
		if tick.d <= 0 {
			return fmt.Errorf("scheduler: the %s tick must be positive, not %s", tick.name, tick.d)
		}
	}
	return nil
}

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
	err = run(ctx, logger, productionTicks())
	stop()
	if err != nil {
		logger.Error("scheduler stopped", "error", err)
		os.Exit(1)
	}
}

func run(ctx context.Context, logger *slog.Logger, t ticks) error {
	if err := t.validate(); err != nil {
		return err
	}
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
	p := newPace(t)
	beats := newHeartbeats(p.isLeading)
	// every wraps a loop's pass with its deadline, heartbeat, trace and
	// duration metric.
	every := func(name string, interval time.Duration, pass func(context.Context) bool) func(context.Context) bool {
		return beats.track(name, interval, timed(name, pass))
	}
	obs := observability.NewObservability(pool, observability.Config{}, logger)

	fleet, err := compute.LoadFleet(ctx, os.Getenv)
	if err != nil {
		return err
	}
	objectStore, err := objectStoreFromEnv(fleet)
	if err != nil {
		return err
	}
	exec := execution.NewExecution(pool)
	sched := scheduling.NewScheduling(pool, exec, logger)
	// Cloud instances reach the server across a network, so they always
	// dial with TLS.
	// The scheduler ships with the server, so the server serves the agent
	// release of this version.
	computeConfig := compute.Config{
		InstallURL: os.Getenv("LAZYCLOUD_INSTALL_URL"), ServerAddress: os.Getenv("LAZYCLOUD_AGENT_SERVER_ADDR"), Fleet: fleet,
	}
	if version != "dev" {
		computeConfig.ServedRelease = version
	}
	if err := computeConfig.CheckFleet(); err != nil {
		return err
	}
	comp := compute.NewCompute(pool, exec, computeConfig)
	store := storage.NewStorage(pool, objectStore, comp)
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
	callbackConfig := callbacks.DefaultCallbackConfig()
	callbackConfig.AllowPrivateTargets = os.Getenv("LAZYCLOUD_CALLBACK_ALLOW_PRIVATE") == "1"
	deliverer, err := callbacks.NewCallbacks(pool, vault, callbackConfig, logger)
	if err != nil {
		return err
	}
	// Build recovery and the layer sweep need no registry: they read and move
	// build state and delete layer pairs.
	buildCPU, err := cpu.ParseCores(cmp.Or(os.Getenv("LAZYCLOUD_BUILD_CPU"), "4"))
	if err != nil {
		return fmt.Errorf("LAZYCLOUD_BUILD_CPU: %w", err)
	}
	im, err := images.NewImages(pool, exec, vault, store, images.Config{BuildCPU: buildCPU, PlatformTransferBackoff: images.PlatformTransferBackoff})
	if err != nil {
		return err
	}
	listener := database.NewListener(session, logger, database.ChannelExecution, database.ChannelImageBuild,
		notifications.Channel, identity.ChannelWorkspace, compute.ChannelCompute, schedules.Channel, database.ChannelCallback)
	fleetWake, cancelFleetWake := listener.Subscribe(compute.ChannelCompute, "")
	defer cancelFleetWake()
	launchWake, cancelLaunchWake := listener.Subscribe(compute.ChannelCompute, "")
	defer cancelLaunchWake()
	connectionWake, cancelConnectionWake := listener.Subscribe(compute.ChannelCompute, "")
	defer cancelConnectionWake()
	capacityWake, cancelCapacityWake := listener.Subscribe(database.ChannelExecution, "")
	defer cancelCapacityWake()
	capacityFleetWake, cancelCapacityFleetWake := listener.Subscribe(compute.ChannelCompute, "")
	defer cancelCapacityFleetWake()
	buildWake, cancelBuildWake := listener.Subscribe(database.ChannelImageBuild, "")
	defer cancelBuildWake()
	accounts, cancelAccounts := newAccountLoops(pool, exec, im, store, listener, logger)
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
		p.lead(ctx, leading, exec.HasLiveWork, logger)
	}
	group.Go(func() error {
		return database.Lead(ctx, session, leaderLock, t.leaderCheck, database.DefaultLeadRetry, leading, logger)
	})
	group.Go(func() error { return p.watch(ctx, exec.HasLiveWork, liveWake, logger) })
	group.Go(func() error { return tel.ServeMetrics(ctx, logger) })
	group.Go(func() error {
		return p.loop(ctx, cadence{every: t.rollup}, nil, nil, every("metrics_rollup", t.rollup, func(ctx context.Context) bool {
			result, err := obs.RollUp(ctx)
			if err != nil {
				logger.ErrorContext(ctx, "metrics rollup pass", "error", err)
			}
			// Behind reruns the pass at once, like a contended lock.
			return result.Behind
		}))
	})
	group.Go(func() error {
		// When a release held back after failed starts may start again. The
		// loop's goroutine alone reads and writes it.
		var retryAt time.Time
		retryDue := func(context.Context) (time.Time, bool, error) { return retryAt, !retryAt.IsZero(), nil }
		return p.loop(ctx, cadence{every: t.tick, quiet: true, due: retryDue}, planWake, nil, every("plan", t.tick, func(ctx context.Context) bool {
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
			retryAt = time.Time{}
			for _, at := range []time.Time{result.RetryAt, serving.RetryAt, pods.RetryAt} {
				if !at.IsZero() && (retryAt.IsZero() || at.Before(retryAt)) {
					retryAt = at
				}
			}
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
		return p.loop(ctx, cadence{every: t.tick, quiet: true}, placeWake, placeNow, every("place", t.tick, func(ctx context.Context) bool {
			result, err := sched.Place(ctx)
			if err != nil {
				logger.ErrorContext(ctx, "placement pass", "error", err)
			}
			return result.Skipped
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: t.tick, quiet: true}, nil, nil, every("workloads", t.tick, func(ctx context.Context) bool {
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
		return p.loop(ctx, cadence{every: t.tick, quiet: true}, nil, nil, every("deadlines", t.tick, func(ctx context.Context) bool {
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
		return p.loop(ctx, cadence{every: t.tick, quiet: true, due: crons.NextFire}, schedulesWake, nil, every("schedules", t.tick, func(ctx context.Context) bool {
			if _, err := crons.Fire(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "schedule pass", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: t.tick, quiet: true, due: deliverer.NextDue}, callbacksWake, nil, every("callbacks", t.tick, func(ctx context.Context) bool {
			if _, err := deliverer.Deliver(ctx); err != nil {
				logger.ErrorContext(ctx, "callback pass", "error", err)
			}
			return false
		}))
	})
	// Purging finished callbacks runs apart, so a long delivery pass never
	// holds it back, and on the leader's timer like other upkeep.
	group.Go(func() error {
		return p.loop(ctx, cadence{every: t.purge}, nil, nil, every("callback_purge", t.purge, func(ctx context.Context) bool {
			if _, err := deliverer.Purge(ctx); err != nil {
				logger.ErrorContext(ctx, "callback purge", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: t.hostLoss}, nil, nil, every("host_loss", t.hostLoss, func(ctx context.Context) bool {
			if _, err := exec.ReleaseLostHosts(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "host loss pass", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		// When the purchases the last pass held for arriving containers may
		// proceed. The loop's goroutine alone reads and writes it.
		var batchAt time.Time
		rerun := func() (time.Time, bool) { return batchAt, !batchAt.IsZero() }
		return p.loop(ctx, cadence{every: t.fleet, rerun: rerun}, capacityWake, capacityFleetWake, timed("fleet_plan", func(ctx context.Context) bool {
			result, err := comp.Plan(ctx, logger)
			if err != nil {
				logger.ErrorContext(ctx, "fleet planning pass", "error", err)
			}
			batchAt = time.Time{}
			if result.BatchWait > 0 {
				batchAt = time.Now().Add(result.BatchWait)
			}
			return result.Skipped
		}))
	})
	// A launch walks EC2's refused pools on a loop of its own, so it holds
	// up no reserve, retirement or preemption.
	group.Go(func() error {
		return p.loop(ctx, cadence{every: t.fleet}, launchWake, nil, timed("fleet_launch", func(ctx context.Context) bool {
			if _, err := comp.Launch(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "launch pass", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: t.fleet}, fleetWake, nil, timed("fleet_hosts", func(ctx context.Context) bool {
			if _, err := comp.Actuate(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "reserve actuator pass", "error", err)
			}
			if _, err := comp.Retire(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "retire pass", "error", err)
			}
			if _, err := comp.Preempt(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "preempt pass", "error", err)
			}
			return false
		}))
	})
	// Connection steps wait on AWS calls in customer accounts, so they run
	// on a loop of their own and hold up no actuation.
	group.Go(func() error {
		return p.loop(ctx, cadence{every: t.connections}, connectionWake, nil, timed("connections", func(ctx context.Context) bool {
			if _, err := comp.AdvanceConnections(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "connection pass", "error", err)
			}
			return false
		}))
	})
	// Each account's networks, made once it takes hosts and found again
	// every half hour, on the leader's timer; the networks lock keeps a
	// replica's start-up pass apart from it.
	group.Go(func() error {
		return p.loop(ctx, cadence{every: t.networks}, nil, nil, timed("fleet_networks", func(ctx context.Context) bool {
			if _, err := comp.EnsureNetworks(ctx, logger); err != nil && ctx.Err() == nil {
				logger.WarnContext(ctx, "fleet networks incomplete", "error", err)
			}
			return false
		}))
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
		return p.loop(ctx, cadence{every: t.quotas}, nil, nil, timed("ec2_quotas", func(ctx context.Context) bool {
			if _, err := comp.RefreshQuotas(ctx, logger); err != nil && ctx.Err() == nil {
				logger.WarnContext(ctx, "ec2 quota refresh incomplete", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: compute.ZoneOfferingInterval}, nil, nil, every("zone_offerings", compute.ZoneOfferingInterval, func(ctx context.Context) bool {
			if _, err := comp.RefreshZoneOfferings(ctx, logger); err != nil && ctx.Err() == nil {
				logger.WarnContext(ctx, "zone offering refresh incomplete", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: t.reconcile}, nil, nil, func(ctx context.Context) bool {
			if err := comp.Reconcile(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "reconcile pass", "error", err)
			}
			return false
		})
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: t.sweep}, nil, nil, every("storage_sweep", t.sweep, func(ctx context.Context) bool {
			if _, err := store.Sweep(ctx, logger); err != nil && ctx.Err() == nil {
				logger.WarnContext(ctx, "storage sweep incomplete", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: t.layerSweep}, nil, nil, every("image_layer_sweep", t.layerSweep, func(ctx context.Context) bool {
			if err := im.SweepLayers(ctx, logger); err != nil && ctx.Err() == nil {
				logger.WarnContext(ctx, "image layer sweep incomplete", "error", err)
			}
			return false
		}))
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: t.buildRecovery}, buildWake, nil, every("build_recovery", t.buildRecovery, func(ctx context.Context) bool {
			if err := im.Recover(ctx, logger); err != nil {
				logger.ErrorContext(ctx, "image build recovery pass", "error", err)
			}
			return false
		}))
	})
	accounts.start(ctx, group, p, beats, t)
	newBillingLoops(pool, session, exec, store, logger).start(ctx, group, p, t)
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
// deletes bytes there. Workspace buckets are named as the fleet says.
func objectStoreFromEnv(fleet compute.Fleet) (storage.Config, error) {
	cfg := storage.Config{
		Endpoint:        os.Getenv("LAZYCLOUD_OBJECT_STORE_ENDPOINT"),
		Region:          os.Getenv("LAZYCLOUD_OBJECT_STORE_REGION"),
		Bucket:          os.Getenv("LAZYCLOUD_OBJECT_STORE_BUCKET"),
		LayerBucket:     os.Getenv("LAZYCLOUD_OBJECT_STORE_LAYER_BUCKET"),
		AccessKeyID:     os.Getenv("LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID"),
		SecretAccessKey: os.Getenv("LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY"),
		DeleteChunk:     storage.DefaultDeleteChunk,
		Workspaces: storage.WorkspaceBuckets{
			Provider:         storage.BucketProvider(os.Getenv("LAZYCLOUD_WORKSPACE_BUCKET_PROVIDER")),
			Prefix:           fleet.BucketPrefix,
			AccountID:        fleet.AccountID,
			GarageAdminURL:   os.Getenv("LAZYCLOUD_GARAGE_ADMIN_URL"),
			GarageAdminToken: os.Getenv("LAZYCLOUD_GARAGE_ADMIN_TOKEN"),
			RoleARN:          os.Getenv("LAZYCLOUD_WORKSPACE_BUCKET_ROLE_ARN"),
		},
	}
	if err := cfg.Validate(); err != nil {
		return cfg, fmt.Errorf("object store (LAZYCLOUD_OBJECT_STORE_*): %w", err)
	}
	return cfg, nil
}
