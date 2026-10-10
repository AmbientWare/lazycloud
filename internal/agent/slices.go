package agent

import (
	"context"
	"fmt"
	"strings"

	systemd "github.com/coreos/go-systemd/v22/dbus"
	"github.com/godbus/dbus/v5"
	"github.com/google/uuid"
	containertypes "github.com/moby/moby/api/types/container"
)

// A container with mounts runs in a systemd slice of its own beside Docker's
// other workloads in lazycloud-workloads.slice, with its mount containers.
// The slice carries the container's memory limit plus its mounters' reserve,
// and its CPU weight, so a mount that outgrows it breaks only that
// container. Docker's systemd cgroup driver places
// containers in a slice the agent makes through systemd, which takes root or
// a polkit grant of org.freedesktop.systemd1.manage-units.

// workloadSlice is the slice of container on this host.
func (a *Agent) workloadSlice(container string) string {
	return a.slicePrefix() + strings.ReplaceAll(container, "-", "") + ".slice"
}

func (a *Agent) slicePrefix() string {
	return "lazycloud-workloads-" + strings.ReplaceAll(a.identity.HostID, "-", "") + "_"
}

func withSystemd(ctx context.Context, fn func(*systemd.Conn) error) error {
	conn, err := systemd.NewWithContext(ctx)
	if err != nil {
		return fmt.Errorf("connect to systemd: %w", err)
	}
	defer conn.Close()
	return fn(conn)
}

// startSlice makes the slice name with the memory limit and CPU weight of
// budget: the container's cgroup settings with its mounters' memory added.
func startSlice(ctx context.Context, name string, budget containertypes.Resources) error {
	return withSystemd(ctx, func(conn *systemd.Conn) error {
		done := make(chan string, 1)
		properties := []systemd.Property{
			systemd.PropDescription("LazyCloud container and its volume mounts"),
			{Name: "MemoryMax", Value: dbus.MakeVariant(uint64(budget.Memory))}, //nolint:gosec // Memory limits are positive.
			{Name: "CPUWeight", Value: dbus.MakeVariant(cpuWeight(budget.CPUShares))},
		}
		if _, err := conn.StartTransientUnitContext(ctx, name, "fail", properties, done); err != nil {
			return fmt.Errorf("create slice %s: %w", name, err)
		}
		return awaitJob(ctx, "create slice "+name, done)
	})
}

// stopSlices stops the named slices and whatever still runs in them.
func stopSlices(ctx context.Context, names ...string) error {
	if len(names) == 0 {
		return nil
	}
	return withSystemd(ctx, func(conn *systemd.Conn) error {
		for _, name := range names {
			done := make(chan string, 1)
			if _, err := conn.StopUnitContext(ctx, name, "fail", done); err != nil {
				return fmt.Errorf("stop slice %s: %w", name, err)
			}
			if err := awaitJob(ctx, "stop slice "+name, done); err != nil {
				return err
			}
		}
		return nil
	})
}

func awaitJob(ctx context.Context, what string, done <-chan string) error {
	select {
	case result := <-done:
		if result != "done" {
			return fmt.Errorf("%s: the job ended %s", what, result)
		}
		return nil
	case <-ctx.Done():
		return fmt.Errorf("%s: %w", what, ctx.Err())
	}
}

// hostSlices lists this host's container slices by container id.
func (a *Agent) hostSlices(ctx context.Context) (map[string]string, error) {
	out := map[string]string{}
	err := withSystemd(ctx, func(conn *systemd.Conn) error {
		units, err := conn.ListUnitsByPatternsContext(ctx, nil, []string{a.slicePrefix() + "*.slice"})
		if err != nil {
			return fmt.Errorf("list container slices: %w", err)
		}
		for _, unit := range units {
			id, err := uuid.Parse(strings.TrimSuffix(strings.TrimPrefix(unit.Name, a.slicePrefix()), ".slice"))
			if err != nil {
				return fmt.Errorf("slice %s names no container: %w", unit.Name, err)
			}
			out[id.String()] = unit.Name
		}
		return nil
	})
	return out, err
}

// cpuWeight is the cgroup v2 CPU weight runc gives a container with shares.
func cpuWeight(shares int64) uint64 {
	return uint64(1 + (shares-2)*9999/262142) //nolint:gosec // containerResources gives at least 2 shares.
}
