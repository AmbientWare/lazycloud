// Command server serves the public API and the host connection, and runs
// administrative bootstrap commands.
//
//	server [serve] [flags]      serve HTTP and gRPC (migrates first)
//	server migrate              apply database migrations
//	server admin <command>      create-user, create-workspace, create-token, create-join-token
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
	"sync"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc"

	"github.com/AmbientWare/lazycloud/internal/api"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/edge"
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

const shutdownGrace = 10 * time.Second

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
	objectStore   storage.Config
	imageTemplate string
	secretsKey    string
	identity      identity.Config
	api           api.Config
	images        images.Config
	edgeAddr      string
	edgeURL       string
	cloudflare    struct{ zone, token string }
}

func serve(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	var cfg serveConfig
	fs.StringVar(&cfg.databaseURL, "database-url", env("LAZYCLOUD_DATABASE_URL", ""), "PostgreSQL URL (LAZYCLOUD_DATABASE_URL)")
	fs.StringVar(&cfg.httpAddr, "http-addr", env("LAZYCLOUD_HTTP_ADDR", "127.0.0.1:8080"), "public API address (LAZYCLOUD_HTTP_ADDR)")
	fs.StringVar(&cfg.grpcAddr, "grpc-addr", env("LAZYCLOUD_GRPC_ADDR", "127.0.0.1:8081"), "host connection address (LAZYCLOUD_GRPC_ADDR)")
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
	fs.StringVar(&cfg.edgeAddr, "edge-addr", env("LAZYCLOUD_EDGE_ADDR", "127.0.0.1:8082"), "workload traffic address (LAZYCLOUD_EDGE_ADDR)")
	fs.StringVar(&cfg.edgeURL, "edge-url", env("LAZYCLOUD_EDGE_URL", "http://lazycloud.localhost:8082"), "public base URL workloads answer under (LAZYCLOUD_EDGE_URL)")
	fs.StringVar(&cfg.cloudflare.zone, "cloudflare-zone-id", env("LAZYCLOUD_CLOUDFLARE_ZONE_ID", ""), "Cloudflare for SaaS zone for custom domains (LAZYCLOUD_CLOUDFLARE_ZONE_ID)")
	if err := fs.Parse(args); err != nil {
		return fmt.Errorf("parse flags: %w", err)
	}
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
	cfg.cloudflare.token = os.Getenv("LAZYCLOUD_CLOUDFLARE_API_TOKEN")
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
		return serveWith(ctx, pool, cfg, tel, logger)
	})
}

func serveWith(ctx context.Context, pool *pgxpool.Pool, cfg serveConfig, tel *telemetry.Telemetry, logger *slog.Logger) error {
	tel.RegisterPool(pool)
	// Billing supplies plan limits once it lands; until then account
	// metrics leave them out.
	obs := observability.NewObservability(pool, observability.Config{Registerer: tel.Registry}, logger)
	changes := observability.NewChanges(pool, observability.DefaultChangesConfig(), tel.Registry, logger)
	listener := database.NewListener(pool, logger, database.ChannelHost, database.ChannelTask, database.ChannelClaim,
		database.ChannelLogs, database.ChannelImageBuild, database.ChannelImageBuildLog, storage.ChannelQueue,
		execution.ChannelContainerLog)
	store := storage.NewStorage(pool, cfg.objectStore)
	exec := execution.NewExecution(pool)
	masterKey, err := secrets.LoadFileKey(cfg.secretsKey)
	if err != nil {
		return err
	}
	vault := secrets.NewSecrets(pool, masterKey)
	im := images.NewImages(pool, exec, cfg.images)
	ident := identity.NewIdentity(pool, cfg.identity)
	if cfg.identity.GitHub.ClientID == "" || cfg.identity.GitHub.ClientSecret == "" {
		logger.WarnContext(ctx, "dashboard sign-in is unavailable: set LAZYCLOUD_GITHUB_CLIENT_ID and LAZYCLOUD_GITHUB_CLIENT_SECRET")
	}
	edgeConfig := edge.Config{URL: cfg.edgeURL}
	if cfg.cloudflare.zone != "" && cfg.cloudflare.token != "" {
		edgeConfig.Domains = edge.NewCloudflare(edge.CloudflareAPI, cfg.cloudflare.zone, cfg.cloudflare.token)
	}
	edges, err := edge.NewEdge(pool, ident, exec, listener, edgeConfig, logger)
	if err != nil {
		return err
	}
	owners := api.Owners{
		Identity: ident, Control: control.NewControl(pool), Storage: store,
		Execution: exec, Images: im, Notifications: notifications.NewNotifications(pool, nil, logger),
		Secrets: vault, Schedules: schedules.NewSchedules(pool, exec), Listener: listener, Edge: edges,
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
	hosts := hostsession.NewServer(compute.NewCompute(pool), exec, store, im, listener, hostsession.Config{
		ImageTemplate: cfg.imageTemplate, TouchInterval: 10 * time.Second,
		Secrets: vault, ContainerAPI: containerAPI, Observability: obs,
	}, logger)
	grpcServer := grpc.NewServer(append(hosts.ServerOptions(), tel.GRPCServerOption())...)
	hostproto.RegisterHostServiceServer(grpcServer, hosts)
	hostproto.RegisterHostDataServer(grpcServer, edges.DataServer(hostsession.HostFrom))
	httpServer := &http.Server{Handler: tel.HTTPHandler(handler, tel.NewHTTPMetrics()), ReadHeaderTimeout: 10 * time.Second}
	edgeServer := &http.Server{Handler: edges, ReadHeaderTimeout: 10 * time.Second}

	var lc net.ListenConfig
	httpListener, err := lc.Listen(ctx, "tcp", cfg.httpAddr)
	if err != nil {
		return fmt.Errorf("listen on %s: %w", cfg.httpAddr, err)
	}
	grpcListener, err := lc.Listen(ctx, "tcp", cfg.grpcAddr)
	if err != nil {
		_ = httpListener.Close()
		return fmt.Errorf("listen on %s: %w", cfg.grpcAddr, err)
	}
	edgeListener, err := lc.Listen(ctx, "tcp", cfg.edgeAddr)
	if err != nil {
		_ = httpListener.Close()
		_ = grpcListener.Close()
		return fmt.Errorf("listen on %s: %w", cfg.edgeAddr, err)
	}
	logger.InfoContext(ctx, "serving", "http", httpListener.Addr().String(), "grpc", grpcListener.Addr().String(),
		"edge", edgeListener.Addr().String(), "edge_url", cfg.edgeURL)

	g, gctx := errgroup.WithContext(ctx)
	g.Go(func() error { return listener.Run(gctx) })
	g.Go(func() error { return changes.Run(gctx) })
	g.Go(func() error { return obs.RunIngest(gctx) })
	g.Go(func() error { return obs.RunStartedPublisher(gctx) })
	g.Go(func() error { return tel.ServeMetrics(gctx, logger) })
	g.Go(func() error { return ident.RunTokenUse(gctx, logger) })
	g.Go(func() error { return edges.Run(gctx) })
	g.Go(func() error {
		if err := edgeServer.Serve(edgeListener); !errors.Is(err, http.ErrServerClosed) {
			return fmt.Errorf("serve edge: %w", err)
		}
		return nil
	})
	g.Go(func() error {
		if err := httpServer.Serve(httpListener); !errors.Is(err, http.ErrServerClosed) {
			return fmt.Errorf("serve http: %w", err)
		}
		return nil
	})
	g.Go(func() error {
		if err := grpcServer.Serve(grpcListener); err != nil {
			return fmt.Errorf("serve grpc: %w", err)
		}
		return nil
	})
	g.Go(func() error {
		<-gctx.Done()
		logger.Info("shutting down")
		shutdownCtx, cancel := context.WithTimeout(context.WithoutCancel(gctx), shutdownGrace)
		defer cancel()
		// Sessions and claim long polls end at once; agents reconnect.
		hosts.Shutdown()
		// Both servers stop accepting and finish their requests, under one
		// grace period; waits and log follows keep a request open until
		// Close ends them. Edge requests still need the agents' data
		// streams, which stay open until the edge is done.
		var servers sync.WaitGroup
		for _, server := range []*http.Server{httpServer, edgeServer} {
			servers.Go(func() {
				if err := server.Shutdown(shutdownCtx); err != nil {
					_ = server.Close()
				}
			})
		}
		servers.Wait()
		// Idle data streams and Listen calls end now.
		edges.Shutdown()
		stopped := make(chan struct{})
		go func() { grpcServer.GracefulStop(); close(stopped) }()
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
