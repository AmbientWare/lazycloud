package main

import (
	"context"
	"log/slog"
	"os"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/notifications"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

// deletionBatch bounds the workspaces one deletion pass advances.
const deletionBatch = 20

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

// newAccountLoops reads the Resend settings from the environment. Without
// LAZYCLOUD_RESEND_API_KEY emails stay queued.
func newAccountLoops(pool *pgxpool.Pool, exec *execution.Execution, im *images.Images, store *storage.Storage, listener *database.Listener, logger *slog.Logger) (*accountLoops, func()) {
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
		storage:       store,
		deliver:       sender != nil,
		emailWake:     emailWake,
		deletionWake:  deletionWake,
		logger:        logger,
	}
	return loops, func() { cancelEmail(); cancelDeletion() }
}

func (a *accountLoops) loops(t ticks) []loop {
	out := []loop{
		{name: "housekeeping", cadence: cadence{every: t.housekeeping}, watch: tracked, pass: steps(a.logger,
			step{what: "identity housekeeping", do: a.identity.Housekeeping},
			step{what: "email purge", do: discardContext(a.notifications.Purge)})},
		// A workspace with objects left reruns the pass at once.
		{name: "workspace deletion", cadence: cadence{every: t.deletion, quiet: true}, wake: a.deletionWake, watch: tracked, pass: a.deleteWorkspaces},
	}
	if a.deliver {
		out = append(out, loop{name: "email delivery", cadence: cadence{every: t.email}, wake: a.emailWake, watch: tracked, pass: func(ctx context.Context) bool {
			result, err := a.notifications.Deliver(ctx)
			if err != nil {
				a.logger.ErrorContext(ctx, "email delivery pass", "error", err)
			}
			return result.More
		}})
	}
	return out
}

// deleteWorkspaces advances each deleting workspace: images fails the
// builds it started and hands finished ones to another workspace, execution
// cancels its running tasks while planning drains its containers, and once none is
// live, storage deletes its objects and identity removes its rows. Every
// step is idempotent, so a crash or a second scheduler replica repeats work
// without harm. An upload presigned before the deletion and finished after
// it leaves bytes under the deleted workspace's prefix. Each workspace
// deletes a bounded chunk of objects per pass; deleteWorkspaces reports
// whether one has objects left.
func (a *accountLoops) deleteWorkspaces(ctx context.Context) (more bool) {
	deleting, err := a.identity.DeletingWorkspaces(ctx, deletionBatch)
	if err != nil {
		a.logger.ErrorContext(ctx, "list deleting workspaces", "error", err)
		return false
	}
	for _, ws := range deleting {
		progressed(ctx)
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
		empty, err := a.storage.DeleteWorkspaceStorage(ctx, a.logger, ws.ID)
		if err != nil {
			a.logger.ErrorContext(ctx, "delete workspace storage", "workspace", ws.Name, "error", err)
			continue
		}
		if !empty {
			more = true
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
			"seconds", time.Since(ws.RequestedAt).Seconds())
	}
	return more
}
