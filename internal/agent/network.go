package agent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/moby/moby/api/pkg/stdcopy"
	containertypes "github.com/moby/moby/api/types/container"
	"github.com/moby/moby/api/types/mount"
	"github.com/moby/moby/client"
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// A container's outbound policy is an nftables filter in its network
// namespace. The agent installs it from a short-lived helper container that
// joins that namespace with only NET_ADMIN and runs the supervisor's
// netfilter subcommand, so workloads never hold the capability themselves.
const (
	kindNetfilter = "netfilter"
	// netfilterTimeout bounds one helper run, image check included.
	netfilterTimeout = time.Minute
	// maxNetfilterRuns bounds helpers running at once on the host.
	maxNetfilterRuns = 8
	// networkPendingFile holds a policy not yet confirmed applied, so an
	// agent that restarts mid-update applies it again.
	networkPendingFile = "network.json"
	netfilterMemory    = 64 << 20
	netfilterPids      = 64
	// maxNetfilterOutput bounds the helper output a failure carries.
	maxNetfilterOutput = 2 << 10
)

// networkState orders a container's policy changes: the newest requested
// policy wins, and one goroutine applies at a time. Policies apply only
// once the container exists; one requested earlier replaces the start's.
type networkState struct {
	mu sync.Mutex
	// want is the newest policy, generation wanted; applied is the
	// generation last applied or given up on.
	want     *hostproto.NetworkPolicy
	wanted   uint64
	applied  uint64
	applying bool
	started  bool
	// wantVersion is the server's version of want; reported is the newest
	// version applied or failed, with failure when it failed.
	wantVersion int32
	reported    int32
	failure     string
}

func (n *networkState) reportedVersion() int32 {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.reported
}

func (n *networkState) reportedFailure() string {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.failure
}

// restricts reports whether policy limits anything; an open policy at start
// needs no filter.
func restricts(policy *hostproto.NetworkPolicy) bool {
	return policy.GetBlock() || len(policy.GetAllow()) > 0
}

// applyStartNetwork applies the start's policy, or a newer one, before the
// supervisor is configured. A failure fails the start.
func (c *container) applyStartNetwork(ctx context.Context, policy *hostproto.NetworkPolicy) error {
	n := &c.network
	n.mu.Lock()
	if n.wanted == 0 && restricts(policy) {
		n.want, n.wanted = policy, 1
	}
	n.started, n.applying = true, true
	n.mu.Unlock()
	return c.drainNetwork(ctx, true)
}

// updateNetwork replaces the container's policy with the server's version
// of it, 0 when unknown. An exited container's update is ignored, and a
// version already reported is only reported again.
func (c *container) updateNetwork(policy *hostproto.NetworkPolicy, version int32) {
	if c.hasExited() {
		c.log.Info("ignoring a network policy update for an exited container")
		return
	}
	n := &c.network
	n.mu.Lock()
	if version > 0 && version <= n.reported {
		n.mu.Unlock()
		c.report()
		return
	}
	n.want, n.wantVersion = policy, version
	n.wanted++
	if !n.started || n.applying {
		n.mu.Unlock()
		return
	}
	n.applying = true
	n.mu.Unlock()
	c.a.goOwned(func(context.Context) { _ = c.drainNetwork(c.work, false) }) //nolint:contextcheck // updates last as long as the container's work
}

// drainNetwork applies wanted policies until the newest is applied. At start
// the first failure is returned; later failures are logged, since the
// container keeps the policy it had.
func (c *container) drainNetwork(ctx context.Context, starting bool) error {
	n := &c.network
	for {
		n.mu.Lock()
		if n.applied >= n.wanted {
			n.applying = false
			n.mu.Unlock()
			return nil
		}
		policy, generation, version := n.want, n.wanted, n.wantVersion
		n.mu.Unlock()
		err := c.applyNetwork(ctx, policy)
		n.mu.Lock()
		n.applied = generation
		if err != nil && starting {
			n.applying = false
		}
		reported := version > n.reported
		if reported {
			n.reported, n.failure = version, ""
			if err != nil {
				n.failure = err.Error()
			}
		}
		n.mu.Unlock()
		if reported {
			c.report()
		}
		switch {
		case err == nil:
		case starting:
			return fmt.Errorf("apply network policy: %w", err)
		case c.hasExited() || ctx.Err() != nil:
			return nil //nolint:nilerr // an exited container's policy no longer matters
		default:
			c.log.Error("applying a network policy update failed; the container keeps its previous policy", "error", err)
		}
	}
}

// applyNetwork records policy as pending, runs the helper and clears the
// record once it applied.
func (c *container) applyNetwork(ctx context.Context, policy *hostproto.NetworkPolicy) error {
	encoded, err := protojson.Marshal(policy)
	if err != nil {
		return fmt.Errorf("encode network policy: %w", err)
	}
	if err := os.MkdirAll(c.dir, 0o700); err != nil {
		return fmt.Errorf("create container directory: %w", err)
	}
	pending := filepath.Join(c.dir, networkPendingFile)
	if err := writeFileAtomic(pending, encoded, 0o600); err != nil {
		return fmt.Errorf("record network policy: %w", err)
	}
	if err := c.a.runNetfilter(ctx, c, policy); err != nil {
		return err
	}
	if err := os.Remove(pending); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("clear network policy record: %w", err)
	}
	c.log.Info("network policy applied", "block", policy.GetBlock(), "allow", len(policy.GetAllow()))
	return nil
}

// resumeNetwork applies again a policy an earlier agent did not confirm.
func (c *container) resumeNetwork() {
	c.network.mu.Lock()
	c.network.started = true
	c.network.mu.Unlock()
	data, err := os.ReadFile(filepath.Join(c.dir, networkPendingFile))
	if errors.Is(err, os.ErrNotExist) {
		return
	}
	policy := &hostproto.NetworkPolicy{}
	if err == nil {
		err = protojson.Unmarshal(data, policy)
	}
	if err != nil {
		c.log.Error("reading the pending network policy failed", "error", err)
		return
	}
	c.updateNetwork(policy, 0)
}

// netfilterRules is the netfilter subcommand's argument.
type netfilterRules struct {
	Block bool     `json:"block"`
	Allow []string `json:"allow"`
}

// runNetfilter runs one policy helper in c's network namespace and waits for
// it to finish.
func (a *Agent) runNetfilter(ctx context.Context, c *container, policy *hostproto.NetworkPolicy) error {
	select {
	case a.netfilters <- struct{}{}:
		defer func() { <-a.netfilters }()
	case <-ctx.Done():
		return fmt.Errorf("wait for a network policy helper: %w", ctx.Err())
	}
	ctx, cancel := context.WithTimeout(ctx, netfilterTimeout)
	defer cancel()
	if _, err := a.images.ensure(ctx, a.cfg.MountImage, nil, ""); err != nil {
		return err
	}
	rules, err := json.Marshal(netfilterRules{Block: policy.GetBlock(), Allow: append([]string{}, policy.GetAllow()...)})
	if err != nil {
		return fmt.Errorf("encode netfilter rules: %w", err)
	}
	labels := maps.Clone(a.cfg.Labels)
	if labels == nil {
		labels = map[string]string{}
	}
	labels[labelHost] = a.identity.HostID
	labels[labelKind] = kindNetfilter
	name := "lazycloud-net-" + c.id + "-" + uuid.NewString()[:8]
	pids := int64(netfilterPids)
	options := client.ContainerCreateOptions{
		Name: name,
		Config: &containertypes.Config{
			Image:      a.cfg.MountImage,
			Entrypoint: []string{containerSupervisor},
			Cmd:        []string{"netfilter", string(rules)},
			Labels:     labels,
			User:       "0",
		},
		HostConfig: &containertypes.HostConfig{
			NetworkMode: containertypes.NetworkMode("container:" + c.networkTarget()),
			CapDrop:     []string{"ALL"},
			CapAdd:      []string{"NET_ADMIN"},
			SecurityOpt: []string{"no-new-privileges"},
			Mounts: []mount.Mount{
				{Type: mount.TypeBind, Source: a.cfg.SupervisorPath, Target: containerSupervisor, ReadOnly: true},
			},
			Resources: containertypes.Resources{Memory: netfilterMemory, PidsLimit: &pids},
		},
	}
	if _, err := a.docker.ContainerCreate(ctx, options); err != nil {
		return fmt.Errorf("create network policy helper: %w", err)
	}
	defer func() {
		if err := a.removeContainer(context.WithoutCancel(ctx), name); err != nil {
			a.log.Warn("removing a network policy helper failed", "name", name, "error", err)
		}
	}()
	if err := a.startDocker(ctx, name, client.ContainerStartOptions{}); err != nil {
		return fmt.Errorf("network policy helper: %w", err)
	}
	wait := a.docker.ContainerWait(ctx, name, client.ContainerWaitOptions{Condition: containertypes.WaitConditionNotRunning})
	var code int64
	select {
	case result := <-wait.Result:
		code = result.StatusCode
	case err := <-wait.Error:
		return fmt.Errorf("wait for the network policy helper: %w", err)
	}
	if code != 0 {
		return fmt.Errorf("the network policy helper exited with code %d: %s", code, a.containerOutput(ctx, name))
	}
	return nil
}

// containerOutput is the end of a stopped container's output, for errors.
func (a *Agent) containerOutput(ctx context.Context, name string) string {
	logs, err := a.docker.ContainerLogs(ctx, name, client.ContainerLogsOptions{ShowStdout: true, ShowStderr: true, Tail: "10"})
	if err != nil {
		return err.Error()
	}
	defer func() { _ = logs.Close() }()
	var out bytes.Buffer
	_, _ = stdcopy.StdCopy(&out, &out, logs)
	text := strings.TrimSpace(strings.ToValidUTF8(out.String(), ""))
	if len(text) > maxNetfilterOutput {
		text = text[len(text)-maxNetfilterOutput:]
	}
	return text
}
