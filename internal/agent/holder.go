package agent

import (
	"context"
	"fmt"
	"maps"

	containertypes "github.com/moby/moby/api/types/container"
	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/platformimages"
)

// A checkpointable container runs in the network namespace of a holder
// container that the agent creates with it and removes with it. Docker
// cannot restore a checkpoint into a container with a network of its own
// (moby#50750: it sets the network up from the task's pid, which a restored
// task does not have yet), and runsc refuses a restore whose spec differs
// from the checkpoint's in the sysctls Docker adds to such a container. A
// container that joins another's namespace gets neither, before and after a
// restore. The holder also takes the container's network policy before the
// container starts, so a restored process never runs unfiltered.
const (
	kindHolder = "network-holder"
	// labelCheckpointable marks a container that runs in its holder's
	// namespace.
	labelCheckpointable = "lazycloud.checkpointable"
	holderPids          = 8
	holderMemory        = 16 << 20
)

func (c *container) holderName() string { return c.dockerName() + "-net" }

// networkTarget is the container whose namespace c's network policy goes
// to.
func (c *container) networkTarget() string {
	if c.checkpointable {
		return c.holderName()
	}
	return c.dockerName()
}

// startHolder creates and starts c's holder, replacing one an earlier
// failed start left. It runs nothing but a sleep, without capabilities.
func (a *Agent) startHolder(ctx context.Context, c *container) error {
	image, err := a.platformImage(ctx, platformimages.Mount)
	if err != nil {
		return fmt.Errorf("network holder: %w", err)
	}
	labels := maps.Clone(a.cfg.Labels)
	if labels == nil {
		labels = map[string]string{}
	}
	labels[labelHost] = a.identity.HostID
	labels[labelKind] = kindHolder
	labels[labelContainer] = c.id
	pids := int64(holderPids)
	options := client.ContainerCreateOptions{
		Name: c.holderName(),
		Config: &containertypes.Config{
			Image:      image,
			Entrypoint: []string{"sleep"},
			Cmd:        []string{"2147483647"},
			Labels:     labels,
			User:       "65534:65534",
		},
		HostConfig: &containertypes.HostConfig{
			CapDrop:        []string{"ALL"},
			SecurityOpt:    []string{"no-new-privileges"},
			ReadonlyRootfs: true,
			Resources:      containertypes.Resources{Memory: holderMemory, PidsLimit: &pids},
		},
	}
	if _, err := a.createContainer(ctx, c.holderName(), options); err != nil {
		return fmt.Errorf("network holder: %w", err)
	}
	if err := a.startDocker(ctx, c.holderName(), client.ContainerStartOptions{}); err != nil {
		return fmt.Errorf("network holder: %w", err)
	}
	return nil
}
