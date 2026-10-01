package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	cerrdefs "github.com/containerd/errdefs"
	"github.com/google/uuid"
	containertypes "github.com/moby/moby/api/types/container"
	"github.com/moby/moby/api/types/mount"
	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/supervisor"
)

// Docker labels on every container the agent creates. The host label scopes
// adoption to this host's containers.
const (
	labelContainer = "lazycloud.container-id"
	labelHost      = "lazycloud.host-id"
	labelHandler   = "lazycloud.handler"
	labelSlots     = "lazycloud.slots"
	// labelRuntime holds the containerRuntime as JSON, so an agent that
	// adopts a container can configure a supervisor that has not connected.
	labelRuntime = "lazycloud.runtime"
)

// pidsLimit bounds processes per container.
const pidsLimit = 4096

func (a *Agent) createAndStart(ctx context.Context, c *container, spec *hostproto.StartContainer, runtime string, binds []mount.Mount, workspaces []string) error {
	env := make([]string, 0, len(spec.GetEnvironment())+len(spec.GetSecrets())+8)
	for _, key := range slices.Sorted(maps.Keys(spec.GetEnvironment())) {
		env = append(env, key+"="+spec.GetEnvironment()[key])
	}
	// Secrets override user values of the same name, as in the reference.
	for _, key := range slices.Sorted(maps.Keys(spec.GetSecrets())) {
		env = append(env, key+"="+spec.GetSecrets()[key])
	}
	// Platform variables come last so they win over user values.
	env = append(env,
		"PYTHONPATH="+containerRuntimeDir,
		"PYTHONUNBUFFERED=1",
		supervisor.SocketEnv+"="+containerLinkDir+"/"+linkSocketName,
		supervisor.APISocketEnv+"="+containerAPISocket,
		"LAZYCLOUD_WORKSPACE="+spec.GetWorkspace(),
		"CONTAINER_ID="+c.id,
	)
	runtimeLabel, err := json.Marshal(c.runtime)
	if err != nil {
		return fmt.Errorf("encode runtime label: %w", err)
	}
	labels := maps.Clone(a.cfg.Labels)
	if labels == nil {
		labels = map[string]string{}
	}
	labels[labelContainer] = c.id
	labels[labelHost] = a.identity.HostID
	labels[labelHandler] = c.handler
	labels[labelSlots] = strconv.Itoa(c.slots)
	if len(workspaces) > 0 {
		labels[labelWorkspaces] = strings.Join(workspaces, ",")
	}
	labels[labelRuntime] = string(runtimeLabel)

	limit := int64(pidsLimit)
	resources := spec.GetResources()
	options := client.ContainerCreateOptions{
		Name: c.dockerName(),
		Config: &containertypes.Config{
			Image:      spec.GetImage(),
			Entrypoint: []string{containerSupervisor},
			Cmd:        []string{},
			Env:        env,
			Labels:     labels,
			WorkingDir: containerWorkspace,
			User:       containerUser(),
		},
		HostConfig: &containertypes.HostConfig{
			Runtime: a.cfg.OCIRuntime,
			Mounts: append([]mount.Mount{
				{Type: mount.TypeBind, Source: runtime, Target: containerRuntimeDir, ReadOnly: true},
				{Type: mount.TypeBind, Source: a.cfg.SupervisorPath, Target: containerSupervisor, ReadOnly: true},
				{Type: mount.TypeBind, Source: c.workspaceDir(), Target: containerWorkspace},
				{Type: mount.TypeBind, Source: c.linkDir(), Target: containerLinkDir},
				// Any container user may create the API socket here.
				{Type: mount.TypeTmpfs, Target: containerAPIDir, TmpfsOptions: &mount.TmpfsOptions{SizeBytes: 1 << 20, Mode: 0o1777}},
			}, binds...),
			Resources:  containerResources(resources, a.capacity, limit),
			StorageOpt: a.diskLimit(resources),
		},
	}
	_, err = a.docker.ContainerCreate(ctx, options)
	if cerrdefs.IsConflict(err) {
		// A container of this id left behind by an earlier failed start.
		if err := a.removeContainer(ctx, c.dockerName()); err != nil {
			return err
		}
		_, err = a.docker.ContainerCreate(ctx, options)
	}
	if err != nil {
		return fmt.Errorf("create container: %w", err)
	}
	if _, err := a.docker.ContainerStart(ctx, c.dockerName(), client.ContainerStartOptions{}); err != nil {
		return fmt.Errorf("start container: %w", err)
	}
	return nil
}

// waitExit blocks until the container is not running. It reports false
// when the wait itself failed.
func (a *Agent) waitExit(ctx context.Context, name string) (bool, error) {
	wait := a.docker.ContainerWait(ctx, name, client.ContainerWaitOptions{Condition: containertypes.WaitConditionNotRunning})
	select {
	case <-wait.Result:
		return true, nil
	case err := <-wait.Error:
		if cerrdefs.IsNotFound(err) {
			return true, nil
		}
		return false, fmt.Errorf("wait for container: %w", err)
	}
}

func (a *Agent) exitState(ctx context.Context, name string) (*containertypes.State, error) {
	inspect, err := a.docker.ContainerInspect(ctx, name, client.ContainerInspectOptions{})
	if err != nil {
		return nil, fmt.Errorf("inspect exited container: %w", err)
	}
	if inspect.Container.State == nil {
		return nil, fmt.Errorf("container %s has no state", name)
	}
	return inspect.Container.State, nil
}

func (a *Agent) stopDocker(ctx context.Context, name string, killAfterSeconds int) error {
	if _, err := a.docker.ContainerStop(ctx, name, client.ContainerStopOptions{Timeout: &killAfterSeconds}); err != nil && !cerrdefs.IsNotFound(err) {
		return fmt.Errorf("stop container: %w", err)
	}
	return nil
}

func (a *Agent) removeContainer(ctx context.Context, name string) error {
	if _, err := a.docker.ContainerRemove(ctx, name, client.ContainerRemoveOptions{Force: true, RemoveVolumes: true}); err != nil && !cerrdefs.IsNotFound(err) {
		return fmt.Errorf("remove container: %w", err)
	}
	return nil
}

// adopt takes over this host's containers from a previous agent. Running
// ones are served again and watched; the supervisor reconnects and restates
// its slots. Exited ones are reported in the next Hello, then removed.
// Directories without a container are leftovers and are deleted.
func (a *Agent) adopt(ctx context.Context) error {
	list, err := a.docker.ContainerList(ctx, client.ContainerListOptions{
		All:     true,
		Filters: client.Filters{}.Add("label", labelHost+"="+a.identity.HostID),
	})
	if err != nil {
		return fmt.Errorf("list containers: %w", err)
	}
	a.volumes.adopt(list.Items)
	for _, summary := range list.Items {
		if summary.Labels[labelKind] == kindMount {
			continue
		}
		id := summary.Labels[labelContainer]
		if _, err := uuid.Parse(id); err != nil {
			continue
		}
		slots, _ := strconv.Atoi(summary.Labels[labelSlots])
		if summary.State == containertypes.StateRunning {
			c := a.newContainer(id, summary.Labels[labelHandler], slots, hostproto.ContainerPhase_CONTAINER_PHASE_STARTING)
			if err := json.Unmarshal([]byte(summary.Labels[labelRuntime]), &c.runtime); err != nil {
				c.log.Warn("container has no readable runtime label", "error", err)
			}
			l, err := listenLink(ctx, c, c.linkDir())
			if err != nil {
				return err
			}
			c.link, c.started = l, true
			disks, err := a.loadLeases(c.id)
			if err != nil {
				c.log.Error("reading disk leases failed", "error", err)
			}
			c.disks.disks = disks
			if len(disks) > 0 {
				a.goOwned(func(context.Context) { c.publishLoop(c.work) }) //nolint:contextcheck // Publishing lasts as long as the container's work.
			}
			l.serve()
			a.track(c)
			a.goOwned(c.watch)
			continue
		}
		c := a.newContainer(id, summary.Labels[labelHandler], slots, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED)
		c.exitedAt = time.Now()
		c.exit = &hostproto.ContainerExit{Reason: hostproto.ExitReason_EXIT_REASON_CRASHED, Message: "exited while the agent was away"}
		if summary.State == containertypes.StateCreated {
			c.exit = &hostproto.ContainerExit{Reason: hostproto.ExitReason_EXIT_REASON_START_FAILED, Message: "the agent restarted before the container started"}
		} else if state, err := a.exitState(ctx, c.dockerName()); err == nil {
			c.exit.ExitCode = int32(state.ExitCode) //nolint:gosec // exit codes fit
			if state.OOMKilled {
				c.exit.Reason = hostproto.ExitReason_EXIT_REASON_OUT_OF_MEMORY
			}
		}
		a.track(c)
	}
	entries, err := os.ReadDir(filepath.Join(a.cfg.StateDir, "containers"))
	if err != nil {
		return fmt.Errorf("list container directories: %w", err)
	}
	for _, entry := range entries {
		if a.lookup(entry.Name()) == nil {
			_ = os.RemoveAll(filepath.Join(a.cfg.StateDir, "containers", entry.Name()))
		}
	}
	return nil
}

// containerUser runs workloads as the agent's own user when the agent is not
// root, so it can remove the files they leave in the workspace. A root agent
// runs them as the image's user.
func containerUser() string {
	if uid := os.Geteuid(); uid != 0 {
		return fmt.Sprintf("%d:%d", uid, os.Getegid())
	}
	return ""
}

// containerResources turns reservations into cgroup settings. CPU shares are
// proportional to the reservation, so under contention a container keeps what
// it reserved; the CPU quota is the burst ceiling, capped at the host. Memory
// reserves the request (memory.low) and kills at the limit, with swap the size
// of the reservation so reclaim can slow a container before the wall.
func containerResources(r *hostproto.Resources, host *hostproto.Capacity, pids int64) containertypes.Resources {
	cpuLimit := r.GetCpuLimitMillis()
	if cpuLimit <= 0 || cpuLimit > host.GetCpuMillis() {
		cpuLimit = host.GetCpuMillis()
	}
	cpuLimit = max(cpuLimit, r.GetCpuMillis())
	memoryLimit := max(r.GetMemoryLimitBytes(), r.GetMemoryBytes())
	return containertypes.Resources{
		CPUShares:         max(r.GetCpuMillis()*1024/1000, 2),
		NanoCPUs:          cpuLimit * 1_000_000,
		MemoryReservation: r.GetMemoryBytes(),
		Memory:            memoryLimit,
		MemorySwap:        memoryLimit + r.GetMemoryBytes(),
		PidsLimit:         &pids,
	}
}
