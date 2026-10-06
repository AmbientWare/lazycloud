package agent

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// Pods, devboxes, sandboxes and shell containers run a command under the
// supervisor instead of runner slots, and never claim tasks.
const (
	// controlSocketName is where every supervisor serves its control API,
	// beside the link socket.
	controlSocketName      = "control.sock"
	containerControlSocket = containerLinkDir + "/" + controlSocketName
	// devboxRoot is where a devbox's root disk is mounted; the supervisor
	// makes it the root of the command and every session.
	devboxRoot       = "/lazycloud/root"
	devboxWorkingDir = "/root"
	// labelPod holds the container's PodProcess without its SSH host key,
	// so an adopted pod is configured the same way again; the key is in
	// sshHostKeyFile in the container's directory.
	labelPod       = "lazycloud.pod"
	labelDocker    = "lazycloud.docker"
	sshHostKeyFile = "ssh_host_key"
)

// podProcess resolves what the supervisor runs for a pod start: the given
// command, or else the image's own entrypoint and command, unless the pod
// idles.
func (a *Agent) podProcess(ctx context.Context, spec *hostproto.StartContainer) (*hostproto.PodProcess, error) {
	pod := spec.GetPod()
	process := &hostproto.PodProcess{
		WorkingDirectory: containerWorkspace,
		Health:           pod.GetHealth(),
		Ssh:              pod.GetSsh(),
		Ports:            pod.GetPorts(),
	}
	if pod.GetDevbox() {
		process.WorkingDirectory = devboxWorkingDir
		process.Root = devboxRoot
	}
	if pod.GetIdle() {
		return process, nil
	}
	process.Command = slices.Clone(pod.GetCommand())
	if len(process.Command) > 0 {
		return process, nil
	}
	inspect, err := a.docker.ImageInspect(ctx, spec.GetImage())
	if err != nil {
		return nil, fmt.Errorf("inspect image %s: %w", spec.GetImage(), err)
	}
	if config := inspect.Config; config != nil {
		process.Command = append(slices.Clone(config.Entrypoint), config.Cmd...)
	}
	if len(process.Command) == 0 {
		return nil, fmt.Errorf("image %s has no entrypoint or command to run", spec.GetImage())
	}
	return process, nil
}

// podLabels records a pod's process for adoption. The SSH host key goes to
// a file only the agent reads, not to a label anyone with Docker access
// sees.
func (c *container) podLabels(labels map[string]string) error {
	if c.docker {
		labels[labelDocker] = "true"
	}
	if c.checkpointable {
		labels[labelCheckpointable] = "true"
	}
	if c.pod == nil {
		return nil
	}
	recorded, ok := proto.Clone(c.pod).(*hostproto.PodProcess)
	if !ok {
		return errors.New("clone pod process")
	}
	if key := recorded.GetSsh().GetHostKey(); len(key) > 0 {
		if err := os.MkdirAll(c.dir, 0o700); err != nil {
			return fmt.Errorf("create container directory: %w", err)
		}
		if err := writeFileAtomic(filepath.Join(c.dir, sshHostKeyFile), key, 0o600); err != nil {
			return fmt.Errorf("record SSH host key: %w", err)
		}
		recorded.Ssh.HostKey = nil
	}
	encoded, err := protojson.Marshal(recorded)
	if err != nil {
		return fmt.Errorf("encode pod label: %w", err)
	}
	labels[labelPod] = string(encoded)
	return nil
}

// podFromLabels restores what podLabels recorded.
func (c *container) podFromLabels(labels map[string]string) error {
	c.docker = labels[labelDocker] == "true"
	c.checkpointable = labels[labelCheckpointable] == "true"
	encoded, ok := labels[labelPod]
	if !ok {
		return nil
	}
	process := &hostproto.PodProcess{}
	if err := protojson.Unmarshal([]byte(encoded), process); err != nil {
		return fmt.Errorf("decode pod label: %w", err)
	}
	if process.GetSsh() != nil {
		key, err := os.ReadFile(filepath.Join(c.dir, sshHostKeyFile))
		if err != nil {
			return fmt.Errorf("read SSH host key: %w", err)
		}
		process.Ssh.HostKey = key
	}
	c.pod = process
	return nil
}

// onCommandExited records that a pod's command ended on its own; the
// supervisor exits with its code next.
func (c *container) onCommandExited(exited *hostproto.CommandExited) {
	if c.pod == nil {
		c.log.Warn("ignoring a command exit from a container that runs no command")
		return
	}
	code := exited.GetExitCode()
	c.mu.Lock()
	c.commandExit = &code
	c.mu.Unlock()
	c.a.layers.served(c.id)
	c.log.Info("pod command exited", "exit_code", code)
}
