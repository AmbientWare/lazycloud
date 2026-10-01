package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/sync/errgroup"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/notifications"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

const (
	// emailTick bounds how long a missed wake or a due retry waits.
	emailTick = 5 * time.Second
	// housekeepingTick paces pruning expired sessions and device codes and
	// purging old email bodies.
	housekeepingTick = time.Minute
	// deletionTick paces workspace deletion while hosts stop containers.
	deletionTick = 2 * time.Second
	// deletionBatch bounds the workspaces one deletion pass advances.
	deletionBatch = 20
)

// accountLoops deliver email, prune identity state and finish workspace
// deletions.
type accountLoops struct {
	identity      *identity.Identity
	notifications *notifications.Notifications
	execution     *execution.Execution
	images        *images.Images
	storage       *storage.Storage
	deliver       bool
	emailWake     <-chan struct{}
	deletionWake  <-chan struct{}
	logger        *slog.Logger
}

// newAccountLoops reads the Resend and object store settings from the
// environment. Without LAZYCLOUD_RESEND_API_KEY emails stay queued.
func newAccountLoops(pool *pgxpool.Pool, exec *execution.Execution, im *images.Images, listener *database.Listener, logger *slog.Logger) (*accountLoops, func(), error) {
	store := storage.Config{
		Endpoint: os.Getenv("LAZYCLOUD_OBJECT_STORE_ENDPOINT"), Region: os.Getenv("LAZYCLOUD_OBJECT_STORE_REGION"),
		Bucket: os.Getenv("LAZYCLOUD_OBJECT_STORE_BUCKET"), AccessKeyID: os.Getenv("LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID"),
		SecretAccessKey: os.Getenv("LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY"),
	}
	if err := store.Validate(); err != nil {
		return nil, nil, fmt.Errorf("workspace deletion needs the object store (LAZYCLOUD_OBJECT_STORE_*): %w", err)
	}
	var sender *notifications.Resend
	if key := os.Getenv("LAZYCLOUD_RESEND_API_KEY"); key != "" {
		from := os.Getenv("LAZYCLOUD_RESEND_FROM")
		if from == "" {
			from = "LazyCloud <noreply@lazycloud.dev>"
		}
		sender = notifications.NewResend(notifications.ResendConfig{APIURL: notifications.ResendAPIURL, APIKey: key, From: from})
	} else {
		logger.Warn("email delivery is off: set LAZYCLOUD_RESEND_API_KEY; invitation emails stay queued")
	}
	emailWake, cancelEmail := listener.Subscribe(notifications.Channel, "")
	deletionWake, cancelDeletion := listener.Subscribe(identity.ChannelWorkspace, "")
	loops := &accountLoops{
		identity:      identity.NewIdentity(pool, identity.Config{}),
		notifications: notifications.NewNotifications(pool, sender, logger),
		execution:     exec,
		images:        im,
		storage:       storage.NewStorage(pool, store),
		deliver:       sender != nil,
		emailWake:     emailWake,
		deletionWake:  deletionWake,
		logger:        logger,
	}
	return loops, func() { cancelEmail(); cancelDeletion() }, nil
}

func (a *accountLoops) start(ctx context.Context, group *errgroup.Group) {
	if a.deliver {
		group.Go(func() error {
			return loop(ctx, emailTick, a.emailWake, nil, func(ctx context.Context) bool {
				result, err := a.notifications.Deliver(ctx)
				if err != nil {
					a.logger.ErrorContext(ctx, "email delivery pass", "error", err)
				}
				return result.More
			})
		})
	}
	group.Go(func() error {
		return loop(ctx, housekeepingTick, nil, nil, func(ctx context.Context) bool {
			if err := a.identity.Housekeeping(ctx, a.logger); err != nil {
				a.logger.ErrorContext(ctx, "identity housekeeping", "error", err)
			}
			if _, err := a.notifications.Purge(ctx); err != nil {
				a.logger.ErrorContext(ctx, "email purge", "error", err)
			}
			return false
		})
	})
	group.Go(func() error {
		return loop(ctx, deletionTick, a.deletionWake, nil, func(ctx context.Context) bool {
			a.deleteWorkspaces(ctx)
			return false
		})
	})
}

// deleteWorkspaces advances each deleting workspace: images fails the
// builds it started and hands finished ones to another workspace, execution
// cancels its running tasks while planning drains its containers, and once none is
// live, storage deletes its objects and identity removes its rows. Every
// step is idempotent, so a crash or a second scheduler replica repeats work
// without harm. An upload presigned before the deletion and finished after
// it leaves bytes under the deleted workspace's prefix.
func (a *accountLoops) deleteWorkspaces(ctx context.Context) {
	deleting, err := a.identity.DeletingWorkspaces(ctx, deletionBatch)
	if err != nil {
		a.logger.ErrorContext(ctx, "list deleting workspaces", "error", err)
		return
	}
	for _, ws := range deleting {
		if err := a.images.ReleaseWorkspace(ctx, ws.ID); err != nil {
			a.logger.ErrorContext(ctx, "release workspace builds", "workspace", ws.Name, "error", err)
			continue
		}
		live, err := a.execution.StopWorkspace(ctx, ws.ID)
		if err != nil {
			a.logger.ErrorContext(ctx, "stop workspace", "workspace", ws.Name, "error", err)
			continue
		}
		if live > 0 {
			continue
		}
		objects, err := a.storage.DeleteWorkspaceObjects(ctx, ws.ID)
		if err != nil {
			a.logger.ErrorContext(ctx, "delete workspace objects", "workspace", ws.Name, "error", err)
			continue
		}
		removed, err := a.identity.FinishWorkspaceDeletion(ctx, ws.ID)
		if err != nil {
			a.logger.ErrorContext(ctx, "remove workspace", "workspace", ws.Name, "error", err)
			continue
		}
		if !removed {
			continue // A container became live; the next pass stops it.
		}
		a.logger.InfoContext(ctx, "workspace deleted", "workspace", ws.Name, "workspace_id", ws.ID.String(),
			"objects", objects, "seconds", time.Since(ws.RequestedAt).Seconds())
	}
}
