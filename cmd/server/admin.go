package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"io"
	iofs "io/fs"
	"os"
	"path/filepath"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// admin runs one bootstrap command against the database. Tokens print to out
// once; only their digests are stored.
func admin(ctx context.Context, args []string, out io.Writer) error {
	if len(args) == 0 {
		return errors.New("admin needs a command: create-user, create-workspace, create-token, create-session, create-join-token or publish-agent-release")
	}
	command, args := args[0], args[1:]
	fs := flag.NewFlagSet("admin "+command, flag.ContinueOnError)
	dbURL := databaseFlag(fs)
	var run func(*pgxpool.Pool) error
	switch command {
	case "create-user":
		email := fs.String("email", "", "user email")
		isAdmin := fs.Bool("admin", false, "reach every workspace")
		run = func(pool *pgxpool.Pool) error {
			if *email == "" {
				return errors.New("-email is required")
			}
			id, err := identity.NewIdentity(pool, identity.Config{}).CreateUser(ctx, *email, *isAdmin)
			if err != nil {
				return err
			}
			return printLine(out, id)
		}
	case "create-workspace":
		name := fs.String("name", "", "workspace name")
		owner := fs.String("owner-email", "", "email of the owning user")
		run = func(pool *pgxpool.Pool) error {
			if *name == "" || *owner == "" {
				return errors.New("-name and -owner-email are required")
			}
			ws, err := identity.NewIdentity(pool, identity.Config{}).CreateWorkspace(ctx, *name, *owner)
			if err != nil {
				return err
			}
			return printLine(out, ws.ID)
		}
	case "create-token":
		email := fs.String("email", "", "email of the token's user")
		workspace := fs.String("workspace", "", "restrict the token to this workspace")
		name := fs.String("name", "", "token name")
		run = func(pool *pgxpool.Pool) error {
			if *email == "" || *name == "" {
				return errors.New("-email and -name are required")
			}
			token, err := identity.NewIdentity(pool, identity.Config{}).CreateToken(ctx, *email, *workspace, *name)
			if err != nil {
				return err
			}
			return printLine(out, token)
		}
	case "create-session":
		email := fs.String("email", "", "email of the session's user")
		run = func(pool *pgxpool.Pool) error {
			if *email == "" {
				return errors.New("-email is required")
			}
			session, err := identity.NewIdentity(pool, identity.Config{}).CreateSession(ctx, *email)
			if err != nil {
				return err
			}
			return printLine(out, session.Token)
		}
	case "create-join-token":
		ttl := fs.Duration("ttl", time.Hour, "time the token stays valid")
		run = func(pool *pgxpool.Pool) error {
			token, _, err := compute.NewCompute(pool, execution.NewExecution(pool), compute.Config{}).CreateJoinToken(ctx, *ttl)
			if err != nil {
				return err
			}
			return printLine(out, token)
		}
	case "publish-agent-release":
		version := fs.String("version", "", "release version")
		dist := fs.String("dist", env("LAZYCLOUD_AGENT_DIST_DIR", ""), "directory holding <version>/lazycloud-agent-linux-<arch>.tar.gz")
		rollout := fs.Int("rollout", 100, "percent of updatable hosts that move to the release; publish again to widen it")
		run = func(pool *pgxpool.Pool) error {
			if !compute.ValidVersion(*version) || *dist == "" {
				return errors.New("-version and -dist are required")
			}
			if *rollout < 0 || *rollout > 100 {
				return errors.New("-rollout must be 0 to 100")
			}
			release := compute.AgentRelease{Version: *version, SHA256: map[string]string{}, RolloutPercent: *rollout}
			for _, arch := range []string{"amd64", "arm64"} {
				digest, err := fileSHA256(filepath.Join(*dist, *version, compute.ArchiveName(arch)))
				if errors.Is(err, iofs.ErrNotExist) {
					continue
				}
				if err != nil {
					return err
				}
				release.SHA256[arch] = digest
			}
			if len(release.SHA256) == 0 {
				return fmt.Errorf("no archive in %s", filepath.Join(*dist, *version))
			}
			if err := compute.NewCompute(pool, execution.NewExecution(pool), compute.Config{}).PublishAgentRelease(ctx, release); err != nil {
				return err
			}
			for arch, digest := range release.SHA256 {
				if err := printLine(out, arch+" "+digest); err != nil {
					return err
				}
			}
			return nil
		}
	default:
		return fmt.Errorf("unknown admin command %q", command)
	}
	if err := fs.Parse(args); err != nil {
		return fmt.Errorf("parse flags: %w", err)
	}
	return withPool(ctx, *dbURL, run)
}

func fileSHA256(path string) (string, error) {
	f, err := os.Open(path) //nolint:gosec // An operator names the file.
	if err != nil {
		return "", fmt.Errorf("open archive: %w", err)
	}
	defer func() { _ = f.Close() }()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", fmt.Errorf("hash archive: %w", err)
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func printLine(out io.Writer, v any) error {
	if _, err := fmt.Fprintln(out, v); err != nil {
		return fmt.Errorf("print: %w", err)
	}
	return nil
}
