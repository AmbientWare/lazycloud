package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// admin runs one bootstrap command against the database. Tokens print to out
// once; only their digests are stored.
func admin(ctx context.Context, args []string, out io.Writer) error {
	if len(args) == 0 {
		return errors.New("admin needs a command: create-user, create-workspace, create-token or create-join-token")
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
			id, err := identity.NewIdentity(pool).CreateUser(ctx, *email, *isAdmin)
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
			ws, err := identity.NewIdentity(pool).CreateWorkspace(ctx, *name, *owner)
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
			token, err := identity.NewIdentity(pool).CreateToken(ctx, *email, *workspace, *name)
			if err != nil {
				return err
			}
			return printLine(out, token)
		}
	case "create-join-token":
		ttl := fs.Duration("ttl", time.Hour, "time the token stays valid")
		run = func(pool *pgxpool.Pool) error {
			token, _, err := compute.NewCompute(pool).CreateJoinToken(ctx, *ttl)
			if err != nil {
				return err
			}
			return printLine(out, token)
		}
	default:
		return fmt.Errorf("unknown admin command %q", command)
	}
	if err := fs.Parse(args); err != nil {
		return fmt.Errorf("parse flags: %w", err)
	}
	return withPool(ctx, *dbURL, run)
}

func printLine(out io.Writer, v any) error {
	if _, err := fmt.Fprintln(out, v); err != nil {
		return fmt.Errorf("print: %w", err)
	}
	return nil
}
