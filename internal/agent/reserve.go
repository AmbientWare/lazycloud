package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"time"

	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/diskengine"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// sleepAttemptFile records the last reserve attempt the agent answered
// ready, so the Hello after a resume or a cold boot names it.
const sleepAttemptFile = "sleep-attempt.json"

// sleepAttempt is the stop a host prepared for: the attempt, the boot it
// was prepared in and the sleep gap then, so the slept time since is the gap
// growth in that boot.
type sleepAttempt struct {
	ID     string        `json:"attempt_id"`
	BootID string        `json:"boot_id"`
	Gap    time.Duration `json:"gap_ns"`
}

func loadSleepAttempt(stateDir string) (sleepAttempt, error) {
	data, err := os.ReadFile(filepath.Join(stateDir, sleepAttemptFile)) //nolint:gosec // A file this package names.
	if errors.Is(err, os.ErrNotExist) {
		return sleepAttempt{}, nil
	}
	if err != nil {
		return sleepAttempt{}, fmt.Errorf("read the sleep attempt: %w", err)
	}
	var attempt sleepAttempt
	if err := json.Unmarshal(data, &attempt); err != nil {
		return sleepAttempt{}, fmt.Errorf("read the sleep attempt: %w", err)
	}
	return attempt, nil
}

// sleptSince is how long the machine slept since attempt was prepared,
// counted only in that boot and only past the noise threshold.
func sleptSince(attempt sleepAttempt, bootID string, gap time.Duration) time.Duration {
	if attempt.ID == "" || attempt.BootID != bootID {
		return 0
	}
	if slept := gap - attempt.Gap; slept > sleepThreshold {
		return slept
	}
	return 0
}

// prepareReserve answers a PrepareReserve off the command loop. Preparations
// run one at a time, and one superseded while it waited is dropped: the
// server only accepts the newest.
func (a *Agent) prepareReserve(request *hostproto.PrepareReserve) {
	a.mu.Lock()
	a.reserveRequest = request.GetAttemptId()
	a.mu.Unlock()
	a.goOwned(func(ctx context.Context) {
		select {
		case a.reserveSlot <- struct{}{}:
		case <-ctx.Done():
			return
		}
		defer func() { <-a.reserveSlot }()
		a.mu.Lock()
		current := a.reserveRequest == request.GetAttemptId()
		a.mu.Unlock()
		if !current {
			return
		}
		ready := a.readyForReserve(ctx, request)
		if ready.GetRefused() != "" {
			a.log.Info("refusing to stop into the reserve", "attempt_id", request.GetAttemptId(), "reason", ready.GetRefused())
		} else {
			a.log.Info("ready to stop into the reserve", "attempt_id", request.GetAttemptId(), "mode", request.GetMode().String())
		}
		a.report(&hostproto.HostMessage{Body: &hostproto.HostMessage_ReserveReady{ReserveReady: ready}})
	})
}

// readyForReserve checks that nothing on the host would be lost or broken by
// a stop and records the attempt. The agent, Docker and the image cache stay.
func (a *Agent) readyForReserve(ctx context.Context, request *hostproto.PrepareReserve) *hostproto.ReserveReady {
	ready := &hostproto.ReserveReady{
		RequestId: request.GetRequestId(), AttemptId: request.GetAttemptId(), BootId: a.bootID, AgentVersion: a.cfg.Version,
	}
	if request.GetGpus() > 0 {
		ready.Gpus = a.driverGPUs(ctx)
	}
	if refused := a.reserveBlocker(ctx); refused != "" {
		ready.Refused = refused
		return ready
	}
	if request.GetMode() == hostproto.ReserveMode_RESERVE_MODE_HIBERNATE {
		if err := writeSleepMarker(request.GetAttemptId(), a.bootID); err != nil {
			ready.Refused = err.Error()
			return ready
		}
	}
	a.mu.Lock()
	attempt := sleepAttempt{ID: request.GetAttemptId(), BootID: a.bootID, Gap: a.clock.gap()}
	a.mu.Unlock()
	data, err := json.Marshal(attempt)
	if err == nil {
		err = writeFileAtomic(filepath.Join(a.cfg.StateDir, sleepAttemptFile), data, 0o600)
	}
	if err != nil {
		ready.Refused = "recording the sleep attempt failed: " + err.Error()
		return ready
	}
	a.mu.Lock()
	a.sleep = attempt
	a.mu.Unlock()
	return ready
}

// reserveBlocker names what keeps the host from stopping, or is empty.
func (a *Agent) reserveBlocker(ctx context.Context) string {
	a.mu.Lock()
	updating := a.updating || a.trial != ""
	live := 0
	for _, c := range a.containers {
		if !c.hasExited() {
			live++
		}
	}
	a.mu.Unlock()
	if updating {
		return "an agent update is in flight"
	}
	if live > 0 {
		return fmt.Sprintf("containers still run here (%d)", live)
	}
	list, err := a.docker.ContainerList(ctx, client.ContainerListOptions{
		All: true, Filters: client.Filters{}.Add("label", labelHost+"="+a.identity.HostID),
	})
	if err != nil {
		return "listing containers failed: " + err.Error()
	}
	for _, summary := range list.Items {
		if kind := summary.Labels[labelKind]; kind == kindMount || kind == kindBucket {
			return "a volume is mounted"
		}
	}
	if len(list.Items) > 0 {
		return fmt.Sprintf("containers remain in Docker (%d)", len(list.Items))
	}
	if leases, err := os.ReadDir(a.leaseDir()); err == nil && len(leases) > 0 {
		return "a disk lease is held"
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return "listing disk leases failed: " + err.Error()
	}
	if a.diskErr == nil {
		disks, err := a.diskEngine.List(ctx)
		if err != nil {
			return "listing disks failed: " + err.Error()
		}
		if slices.ContainsFunc(disks, func(d diskengine.LocalDisk) bool { return d.Attached }) {
			return "a disk is attached"
		}
	}
	return ""
}

// driverGPUs counts the offered GPUs the driver reports now.
func (a *Agent) driverGPUs(ctx context.Context) int32 {
	found := int32(0)
	for _, gpu := range detectGPUs(ctx) {
		if slices.ContainsFunc(a.gpus, func(offered gpuDevice) bool { return offered.UUID == gpu.UUID }) {
			found++
		}
	}
	return found
}

// writeSleepMarker puts the attempt into the kernel log, where the console
// output EC2 keeps after a hibernation shows it ran before the image was
// written.
func writeSleepMarker(attempt, boot string) error {
	kmsg, err := os.OpenFile("/dev/kmsg", os.O_WRONLY, 0)
	if err != nil {
		return fmt.Errorf("open the kernel log for the sleep marker: %w", err)
	}
	_, err = fmt.Fprintf(kmsg, "lazycloud-sleep attempt=%s boot=%s\n", attempt, boot)
	if closeErr := kmsg.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return fmt.Errorf("write the sleep marker: %w", err)
	}
	return nil
}
