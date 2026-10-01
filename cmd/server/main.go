// Command server serves the public API and the host connection, and runs
// administrative bootstrap commands.
//
//	server [serve] [flags]      serve HTTP and gRPC (migrates first)
//	server migrate              apply database migrations
//	server admin <command>      create-user, create-workspace, create-token, set-complimentary, create-join-token
//
// On SIGTERM the server reports not ready on /readyz, keeps serving for the
// drain delay so load balancers stop routing to it, then ends host sessions
// and gives open requests the shutdown grace to finish.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"

	"github.com/AmbientWare/lazycloud/internal/api"
	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/hostsession"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/notifications"
	"github.com/AmbientWare/lazycloud/internal/observability"
	"github.com/AmbientWare/lazycloud/internal/schedules"
	"github.com/AmbientWare/lazycloud/internal/secrets"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// shutdownGrace bounds how long open requests and RPCs may finish after the
// listeners close. Pod termination grace must cover the drain delay plus this.
const shutdownGrace = 10 * time.Second

// version is stamped by the image build with -ldflags "-X main.version=..."
// and logged at start.
var version = "dev"

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	err := run(ctx, os.Args[1:], os.Stdout)
	stop()
	if err != nil {
		fmt.Fprintln(os.Stderr, "server:", err)
		os.Exit(1)
	}
}

func run(ctx context.Context, args []string, out io.Writer) error {
	command := "serve"
	if len(args) > 0 && args[0] != "" && args[0][0] != '-' {
		command, args = args[0], args[1:]
	}
	switch command {
	case "serve":
		return serve(ctx, args)
	case "migrate":
		fs := flag.NewFlagSet("migrate", flag.ContinueOnError)
		dbURL := databaseFlag(fs)
		if err := fs.Parse(args); err != nil {
			return fmt.Errorf("parse flags: %w", err)
		}
		return withPool(ctx, *dbURL, func(pool *pgxpool.Pool) error { return database.Migrate(ctx, pool) })
	case "admin":
		return admin(ctx, args, out)
	}
	return fmt.Errorf("unknown command %q; want serve, migrate or admin", command)
}

func env(name, fallback string) string {
	if v, ok := os.LookupEnv(name); ok {
		return v
	}
	return fallback
}

// billingConfig reads the Stripe credentials from the environment, so they
// stay out of the process arguments.
func billingConfig(publicURL string) billing.Config {
	return billing.Config{PublicURL: publicURL, Stripe: billing.StripeConfig{
		SecretKey: os.Getenv("LAZYCLOUD_STRIPE_API_KEY"), WebhookSecret: os.Getenv("LAZYCLOUD_STRIPE_WEBHOOK_SECRET"),
	}}
}

func databaseFlag(fs *flag.FlagSet) *string {
	return fs.String("database-url", env("LAZYCLOUD_DATABASE_URL", ""), "PostgreSQL URL (LAZYCLOUD_DATABASE_URL)")
}

func withPool(ctx context.Context, url string, fn func(*pgxpool.Pool) error) error {
	if url == "" {
		return errors.New("the database URL is required: set LAZYCLOUD_DATABASE_URL or -database-url")
	}
	pool, err := database.Open(ctx, url)
	if err != nil {
		return err
	}
	defer pool.Close()
	return fn(pool)
}

type serveConfig struct {
	databaseURL   string
	httpAddr      string
	grpcAddr      string
	healthAddr    string
	objectStore   storage.Config
	imageTemplate string
	secretsKey    string
	identity      identity.Config
	api           api.Config
	images        images.Config
	billing       billing.Config
	compute       compute.Config
	grpcCert      string
	grpcKey       string
	// drainDelay is how long the server keeps serving after shutdown
	// starts while /readyz reports draining.
	drainDelay time.Duration
}

func serve(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	var cfg serveConfig
	fs.StringVar(&cfg.databaseURL, "database-url", env("LAZYCLOUD_DATABASE_URL", ""), "PostgreSQL URL (LAZYCLOUD_DATABASE_URL)")
	fs.StringVar(&cfg.httpAddr, "http-addr", env("LAZYCLOUD_HTTP_ADDR", "127.0.0.1:8080"), "public API address (LAZYCLOUD_HTTP_ADDR)")
	fs.StringVar(&cfg.grpcAddr, "grpc-addr", env("LAZYCLOUD_GRPC_ADDR", "127.0.0.1:8081"), "host connection address (LAZYCLOUD_GRPC_ADDR)")
	fs.StringVar(&cfg.healthAddr, "health-addr", env("LAZYCLOUD_HEALTH_ADDR", ""), "address of /healthz and /readyz, unset for none (LAZYCLOUD_HEALTH_ADDR)")
	fs.StringVar(&cfg.objectStore.Endpoint, "object-store-endpoint", env("LAZYCLOUD_OBJECT_STORE_ENDPOINT", ""), "S3-compatible endpoint URL; empty is AWS S3 (LAZYCLOUD_OBJECT_STORE_ENDPOINT)")
	fs.StringVar(&cfg.objectStore.Region, "object-store-region", env("LAZYCLOUD_OBJECT_STORE_REGION", ""), "object store region (LAZYCLOUD_OBJECT_STORE_REGION)")
	fs.StringVar(&cfg.objectStore.Bucket, "object-store-bucket", env("LAZYCLOUD_OBJECT_STORE_BUCKET", ""), "bucket for source archives (LAZYCLOUD_OBJECT_STORE_BUCKET)")
	fs.StringVar(&cfg.objectStore.AccessKeyID, "object-store-access-key-id", env("LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID", ""), "object store access key id; empty uses the AWS default credential chain (LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID)")
	fs.StringVar((*string)(&cfg.objectStore.Workspaces.Provider), "workspace-bucket-provider", env("LAZYCLOUD_WORKSPACE_BUCKET_PROVIDER", ""), "garage or aws: creates the per-workspace buckets of volumes and disks (LAZYCLOUD_WORKSPACE_BUCKET_PROVIDER)")
	fs.StringVar(&cfg.objectStore.Workspaces.Prefix, "workspace-bucket-prefix", env("LAZYCLOUD_WORKSPACE_BUCKET_PREFIX", "lazycloud-ws"), "prefix of workspace bucket names (LAZYCLOUD_WORKSPACE_BUCKET_PREFIX)")
	fs.StringVar(&cfg.objectStore.Workspaces.GarageAdminURL, "garage-admin-url", env("LAZYCLOUD_GARAGE_ADMIN_URL", ""), "Garage admin API URL (LAZYCLOUD_GARAGE_ADMIN_URL)")
	fs.StringVar(&cfg.objectStore.Workspaces.RoleARN, "workspace-bucket-role-arn", env("LAZYCLOUD_WORKSPACE_BUCKET_ROLE_ARN", ""), "role STS issues host credentials for (LAZYCLOUD_WORKSPACE_BUCKET_ROLE_ARN)")
	fs.StringVar(&cfg.imageTemplate, "image-template", env("LAZYCLOUD_IMAGE_TEMPLATE", "docker.io/library/python:{version}-slim"), "container image per Python version (LAZYCLOUD_IMAGE_TEMPLATE)")
	fs.StringVar(&cfg.secretsKey, "secrets-key-file", env("LAZYCLOUD_SECRETS_KEY_FILE", ""), "32-byte master key file that wraps secret data keys (LAZYCLOUD_SECRETS_KEY_FILE)")
	fs.StringVar(&cfg.identity.PublicURL, "public-url", env("LAZYCLOUD_PUBLIC_URL", "http://127.0.0.1:8080"), "dashboard origin for sign-in, device login and invitation links (LAZYCLOUD_PUBLIC_URL)")
	fs.StringVar(&cfg.identity.GitHub.ClientID, "github-client-id", env("LAZYCLOUD_GITHUB_CLIENT_ID", ""), "GitHub App client id for dashboard sign-in (LAZYCLOUD_GITHUB_CLIENT_ID)")
	fs.StringVar(&cfg.api.ClientReleaseVersion, "client-release-version", env("LAZYCLOUD_CLIENT_RELEASE_VERSION", ""), "CLI version older clients are told to update to (LAZYCLOUD_CLIENT_RELEASE_VERSION)")
	fs.StringVar(&cfg.images.Registry, "image-registry", env("LAZYCLOUD_IMAGE_REGISTRY", ""), "registry host[:port] that builds publish to (LAZYCLOUD_IMAGE_REGISTRY)")
	fs.StringVar(&cfg.images.Repository, "image-repository", env("LAZYCLOUD_IMAGE_REPOSITORY", "lazycloud"), "path under the registry for images and build cache (LAZYCLOUD_IMAGE_REPOSITORY)")
	fs.BoolVar(&cfg.images.Insecure, "image-registry-insecure", env("LAZYCLOUD_IMAGE_REGISTRY_INSECURE", "") == "true", "the image registry speaks plain HTTP (LAZYCLOUD_IMAGE_REGISTRY_INSECURE)")
	fs.StringVar(&cfg.compute.ServerAddress, "agent-server-addr", env("LAZYCLOUD_AGENT_SERVER_ADDR", ""), "host:port agents dial; defaults to -grpc-addr (LAZYCLOUD_AGENT_SERVER_ADDR)")
	fs.StringVar(&cfg.grpcCert, "grpc-tls-cert", env("LAZYCLOUD_GRPC_TLS_CERT", ""), "PEM certificate chain the host connection serves; empty serves plaintext for loopback or a TLS-terminating ingress (LAZYCLOUD_GRPC_TLS_CERT)")
	fs.StringVar(&cfg.grpcKey, "grpc-tls-key", env("LAZYCLOUD_GRPC_TLS_KEY", ""), "PEM private key for -grpc-tls-cert (LAZYCLOUD_GRPC_TLS_KEY)")
	fs.StringVar(&cfg.compute.InstallURL, "install-url", env("LAZYCLOUD_INSTALL_URL", ""), "origin hosts download the agent from; defaults to -public-url (LAZYCLOUD_INSTALL_URL)")
	fs.StringVar(&cfg.api.AgentDistDir, "agent-dist-dir", env("LAZYCLOUD_AGENT_DIST_DIR", ""), "agent release archives by version (LAZYCLOUD_AGENT_DIST_DIR)")
	drainDelay := fs.String("drain-delay", env("LAZYCLOUD_DRAIN_DELAY", "0s"), "time to keep serving after SIGTERM while /readyz reports draining (LAZYCLOUD_DRAIN_DELAY)")
	if err := fs.Parse(args); err != nil {
		return fmt.Errorf("parse flags: %w", err)
	}
	var err error
	if cfg.drainDelay, err = time.ParseDuration(*drainDelay); err != nil || cfg.drainDelay < 0 {
		return fmt.Errorf("drain delay %q is not a non-negative duration", *drainDelay)
	}
	if cfg.compute.ServerAddress == "" {
		cfg.compute.ServerAddress = cfg.grpcAddr
	}
	if (cfg.grpcCert == "") != (cfg.grpcKey == "") {
		return errors.New("set both -grpc-tls-cert and -grpc-tls-key, or neither")
	}
	cfg.compute.ServerPlaintext = compute.PlaintextAgents(cfg.compute.ServerAddress, cfg.grpcCert != "")
	if cfg.compute.InstallURL == "" {
		cfg.compute.InstallURL = cfg.identity.PublicURL
	}
	fleet, err := compute.LoadFleet(ctx, os.Getenv)
	if err != nil {
		return err
	}
	cfg.compute.Fleet = fleet
	if cfg.images.Registry == "" {
		return errors.New("the image registry is required: set LAZYCLOUD_IMAGE_REGISTRY or -image-registry")
	}
	cfg.images.ManagedBase = cfg.imageTemplate
	if user := os.Getenv("LAZYCLOUD_IMAGE_REGISTRY_USERNAME"); user != "" {
		cfg.images.Auth = &images.Auth{Username: user, Password: os.Getenv("LAZYCLOUD_IMAGE_REGISTRY_PASSWORD")}
	}
	// Secrets stay out of the process arguments.
	cfg.identity.GitHub.ClientSecret = os.Getenv("LAZYCLOUD_GITHUB_CLIENT_SECRET")
	cfg.api.ResendWebhookSecret = os.Getenv("LAZYCLOUD_RESEND_WEBHOOK_SECRET")
	cfg.api.PublicURL = cfg.identity.PublicURL
	cfg.billing = billingConfig(cfg.identity.PublicURL)
	cfg.objectStore.SecretAccessKey = os.Getenv("LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY")
	cfg.objectStore.Workspaces.GarageAdminToken = os.Getenv("LAZYCLOUD_GARAGE_ADMIN_TOKEN")
	if err := cfg.objectStore.Validate(); err != nil {
		return fmt.Errorf("object store: %w", err)
	}
	if cfg.secretsKey == "" {
		return errors.New("the secrets key file is required: set LAZYCLOUD_SECRETS_KEY_FILE or -secrets-key-file")
	}
	format, err := telemetry.LogFormatFromEnv(telemetry.LogText)
	if err != nil {
		return err
	}
	logger := telemetry.NewLogger(os.Stderr, format, "server")
	telemetryConfig, err := telemetry.ConfigFromEnv("server", "")
	if err != nil {
		return err
	}
	tel, err := telemetry.New(ctx, telemetryConfig)
	if err != nil {
		return err
	}
	defer func() { _ = tel.Shutdown(context.WithoutCancel(ctx)) }()
	return withPool(ctx, cfg.databaseURL, func(pool *pgxpool.Pool) error {
		if err := database.Migrate(ctx, pool); err != nil {
			return err
		}
		var ls listeners
		// The servers close them too; this covers a failed setup.
		defer func() { ls.close() }()
		var lc net.ListenConfig
		var err error
		for _, l := range []struct {
			addr string
			into *net.Listener
		}{{cfg.httpAddr, &ls.http}, {cfg.grpcAddr, &ls.grpc}, {cfg.healthAddr, &ls.health}} {
			if l.addr == "" {
				continue
			}
			if *l.into, err = lc.Listen(ctx, "tcp", l.addr); err != nil {
				return fmt.Errorf("listen on %s: %w", l.addr, err)
			}
		}
		return serveWith(ctx, pool, cfg, tel, logger, ls)
	})
}

// listeners are the server's sockets; health is nil without a health
// address.
type listeners struct {
	http, grpc, health net.Listener
}

func (ls listeners) close() {
	for _, l := range []net.Listener{ls.http, ls.grpc, ls.health} {
		if l != nil {
			_ = l.Close()
		}
	}
}

// serveWith serves on the listeners until ctx ends, then drains.
func serveWith(ctx context.Context, pool *pgxpool.Pool, cfg serveConfig, tel *telemetry.Telemetry, logger *slog.Logger, ls listeners) error {
	tel.RegisterPool(pool)
	// Billing supplies plan limits once it lands; until then account
	// metrics leave them out.
	obs := observability.NewObservability(pool, observability.Config{Registerer: tel.Registry}, logger)
	changes := observability.NewChanges(pool, observability.DefaultChangesConfig(), tel.Registry, logger)
	listener := database.NewListener(pool, logger, database.ChannelHost, database.ChannelTask, database.ChannelClaim,
		database.ChannelLogs, database.ChannelImageBuild, database.ChannelImageBuildLog, storage.ChannelQueue)
	cfg.objectStore.BrowserOrigin = cfg.identity.PublicURL
	store := storage.NewStorage(pool, cfg.objectStore)
	// The dashboard reads and writes artifacts with presigned URLs. A store
	// that refuses the rule only costs those browser transfers.
	if err := store.AllowBrowserAccess(ctx); err != nil {
		logger.WarnContext(ctx, "dashboard transfers of artifacts will fail", "error", err)
	}
	exec := execution.NewExecution(pool)
	masterKey, err := secrets.LoadFileKey(cfg.secretsKey)
	if err != nil {
		return err
	}
	vault := secrets.NewSecrets(pool, masterKey)
	im := images.NewImages(pool, exec, cfg.images)
	ident := identity.NewIdentity(pool, cfg.identity)
	comp := compute.NewCompute(pool, exec, cfg.compute)
	if cfg.identity.GitHub.ClientID == "" || cfg.identity.GitHub.ClientSecret == "" {
		logger.WarnContext(ctx, "dashboard sign-in is unavailable: set LAZYCLOUD_GITHUB_CLIENT_ID and LAZYCLOUD_GITHUB_CLIENT_SECRET")
	}
	bill := billing.NewBilling(pool, cfg.billing, logger)
	if cfg.billing.Stripe.SecretKey == "" {
		logger.WarnContext(ctx, "payments are unavailable: set LAZYCLOUD_STRIPE_API_KEY and LAZYCLOUD_STRIPE_WEBHOOK_SECRET")
	}
	owners := api.Owners{
		Identity: ident, Control: control.NewControl(pool), Storage: store,
		Execution: exec, Images: im, Notifications: notifications.NewNotifications(pool, nil, logger),
		Secrets: vault, Schedules: schedules.NewSchedules(pool, exec), Billing: bill, Listener: listener, Compute: comp,
		Observability: obs, Changes: changes,
	}
	handler, err := api.NewHandler(owners, cfg.api, logger)
	if err != nil {
		return err
	}
	containerAPI, err := api.NewContainerHandler(owners, cfg.api, logger)
	if err != nil {
		return err
	}
	hosts := hostsession.NewServer(comp, exec, store, im, listener, hostsession.Config{
		ImageTemplate: cfg.imageTemplate, TouchInterval: 10 * time.Second,
		Secrets: vault, ContainerAPI: containerAPI, Observability: obs,
	}, logger)
	grpcOptions := append(hosts.ServerOptions(), tel.GRPCServerOption())
	if cfg.grpcCert != "" {
		creds, err := credentials.NewServerTLSFromFile(cfg.grpcCert, cfg.grpcKey)
		if err != nil {
			return fmt.Errorf("load host connection certificate: %w", err)
		}
		grpcOptions = append(grpcOptions, grpc.Creds(creds))
	}
	grpcServer := grpc.NewServer(grpcOptions...)
	hostproto.RegisterHostServiceServer(grpcServer, hosts)
	probes := &health{}
	httpServer := &http.Server{Handler: tel.HTTPHandler(handler, tel.NewHTTPMetrics()), ReadHeaderTimeout: 10 * time.Second}
	logger.InfoContext(ctx, "serving", "version", version, "http", ls.http.Addr().String(), "grpc", ls.grpc.Addr().String())

	g, gctx := errgroup.WithContext(ctx)
	// Requests and RPCs still open during the drain wait on NOTIFY wake-ups,
	// stream changes, send observations and record token use, so this work
	// stops only after both servers have stopped.
	background, stopBackground := context.WithCancel(context.WithoutCancel(ctx))
	defer stopBackground()
	g.Go(func() error { return listener.Run(background) })
	g.Go(func() error { return changes.Run(background) })
	g.Go(func() error { return obs.RunIngest(background) })
	g.Go(func() error { return obs.RunStartedPublisher(background) })
	g.Go(func() error { return tel.ServeMetrics(background, logger) })
	g.Go(func() error { return ident.RunTokenUse(background, logger) })
	g.Go(func() error {
		if err := httpServer.Serve(ls.http); !errors.Is(err, http.ErrServerClosed) {
			return fmt.Errorf("serve http: %w", err)
		}
		return nil
	})
	g.Go(func() error {
		if err := grpcServer.Serve(ls.grpc); err != nil {
			return fmt.Errorf("serve grpc: %w", err)
		}
		return nil
	})
	var healthServer *http.Server
	if ls.health != nil {
		healthServer = &http.Server{Handler: probes.handler(), ReadHeaderTimeout: 5 * time.Second}
		g.Go(func() error {
			if err := healthServer.Serve(ls.health); !errors.Is(err, http.ErrServerClosed) {
				return fmt.Errorf("serve health: %w", err)
			}
			return nil
		})
	}
	g.Go(func() error {
		<-gctx.Done()
		defer stopBackground()
		if healthServer != nil {
			defer func() { _ = healthServer.Close() }()
		}
		probes.draining.Store(true)
		if cfg.drainDelay > 0 {
			logger.Info("draining", "delay", cfg.drainDelay.String())
			delay := time.NewTimer(cfg.drainDelay)
			<-delay.C
		}
		logger.Info("shutting down")
		// Sessions and claim long polls end at once; agents reconnect.
		hosts.Shutdown()
		stopped := make(chan struct{})
		go func() { grpcServer.GracefulStop(); close(stopped) }()
		shutdownCtx, cancel := context.WithTimeout(context.WithoutCancel(gctx), shutdownGrace)
		defer cancel()
		// Waits and log follows keep a request open; Close ends them after
		// the grace period.
		if err := httpServer.Shutdown(shutdownCtx); err != nil {
			_ = httpServer.Close()
		}
		select {
		case <-stopped:
		case <-shutdownCtx.Done():
			grpcServer.Stop()
			<-stopped
		}
		return nil
	})
	err = g.Wait()
	hosts.Wait()
	if err != nil {
		return fmt.Errorf("serve: %w", err)
	}
	return nil
}
