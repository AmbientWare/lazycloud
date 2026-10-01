package control

import (
	"fmt"
	"net/netip"
	"slices"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// Pod defaults, as the reference resolved them: pods and sandboxes stay up
// ten idle minutes and are public, devboxes stay half an hour, keep their
// machine and always serve SSH.
const (
	podKeepWarmSeconds    = 600
	devboxKeepWarmSeconds = 1800
	maxAllowList          = 10
)

// resolvePod checks and fills the pod section of a resolved spec. A spec
// without one must name a handler.
func resolvePod(spec apitypes.FunctionSpec, out *apitypes.FunctionSpec) error {
	invalid := func(reason string) error { return &InvalidSpecError{Function: spec.Name, Reason: reason} }
	if err := resolveDisks(spec, out); err != nil {
		return err
	}
	if spec.Pod == nil {
		if spec.Handler == nil {
			return invalid("handler is required")
		}
		if spec.Checkpoint != nil && (spec.Checkpoint.ReadinessPath != nil || spec.Checkpoint.ReadinessPort != nil) {
			return invalid("checkpoint readiness applies to pods; a handler is snapshotted once it has loaded")
		}
		return nil
	}
	p := *spec.Pod
	switch {
	case !p.Kind.Valid():
		return invalid(fmt.Sprintf("unknown pod kind %q", p.Kind))
	case spec.Handler != nil:
		return invalid("a pod runs its command, not a handler")
	case spec.Http != nil:
		return invalid("a pod serves its ports, not an HTTP handler")
	case spec.Cron != nil:
		return invalid("schedules apply to functions, not pods")
	case spec.InProcess != nil && *spec.InProcess:
		return invalid("in_process applies to functions")
	}
	keepWarm := podKeepWarmSeconds
	if p.Kind == apitypes.PodKindDevbox {
		keepWarm = devboxKeepWarmSeconds
	}
	if spec.KeepWarmSeconds != nil {
		keepWarm = *spec.KeepWarmSeconds
	}
	out.KeepWarmSeconds = &keepWarm
	// Pods answer without a token unless they ask for one.
	out.Authorized = orDefault(spec.Authorized, false)

	ports := map[string]int{}
	if p.Ports != nil {
		ports = *p.Ports
	}
	p.Ports = &ports
	p.Tcp = orDefault(p.Tcp, false)
	p.Ssh = orDefault(p.Ssh, false)
	p.BlockNetwork = orDefault(p.BlockNetwork, false)
	allow, err := allowList(p.AllowList)
	if err != nil {
		return invalid(err.Error())
	}
	p.AllowList = &allow
	if p.Command == nil {
		p.Command = &[]string{}
	}
	if h := p.HealthCheck; h != nil && h.Port == nil {
		first, ok := firstPort(ports)
		if !ok {
			return invalid("health_check needs a port: declare ports or set health_check.port")
		}
		p.HealthCheck = &apitypes.HealthCheck{Path: h.Path, Port: &first}
	}
	if *p.Tcp {
		if *out.Authorized {
			return invalid("raw TCP ingress requires a public Pod with authorized=False")
		}
		if len(ports) == 0 {
			return invalid("tcp needs a port to serve")
		}
	}
	var root *apitypes.DiskMountSpec
	if out.Disks != nil {
		for n, d := range *out.Disks {
			if d.MountPath == "/" {
				root = &(*out.Disks)[n]
			}
		}
	}
	switch p.Kind {
	case apitypes.PodKindDevbox:
		if spec.Pod.Ssh != nil && !*spec.Pod.Ssh {
			return invalid("a devbox is reached over SSH and cannot turn ssh off")
		}
		p.Ssh = new(true)
		if root == nil {
			return invalid("a devbox needs a root disk; give its size")
		}
		if root.Name != spec.Name {
			return invalid("a devbox's root disk is named after the devbox")
		}
		if *p.Tcp {
			return invalid("a devbox is reached over SSH, not raw TCP")
		}
		if out.Placement == nil || out.Placement.Preemptible == nil {
			placement := apitypes.Placement{}
			if out.Placement != nil {
				placement = *out.Placement
			}
			placement.Preemptible = new(false)
			out.Placement = &placement
		}
	case apitypes.PodKindSandbox:
		if *p.Tcp || *p.Ssh {
			return invalid("a sandbox serves its ports over HTTP; tcp and ssh apply to pods")
		}
		if spec.Checkpoint != nil {
			return invalid("a sandbox is snapshotted with snapshot_memory, not checkpoint_enabled")
		}
		fallthrough
	case apitypes.PodKindPod:
		if root != nil {
			return invalid("only a devbox mounts a disk at /")
		}
	}
	if c := spec.Checkpoint; c != nil && (c.ReadinessPath == nil || c.ReadinessPort == nil) {
		return invalid("checkpoint_enabled Pods require checkpoint_readiness_path and checkpoint_readiness_port")
	}
	if keepWarm == -1 && p.Kind == apitypes.PodKindSandbox {
		return invalid("a sandbox's idle lifetime is set per instance; keep_warm_seconds must not be -1")
	}
	out.Pod = &p
	return nil
}

// resolveDisks checks disk declarations; a workload with one runs one
// container at a time.
func resolveDisks(spec apitypes.FunctionSpec, out *apitypes.FunctionSpec) error {
	if spec.Disks == nil || len(*spec.Disks) == 0 {
		out.Disks = nil
		return nil
	}
	seen := map[string]bool{}
	paths := map[string]bool{}
	for _, d := range *spec.Disks {
		switch {
		case seen[d.Name]:
			return &InvalidSpecError{Function: spec.Name, Reason: "disk " + d.Name + " is declared twice"}
		case paths[d.MountPath]:
			return &InvalidSpecError{Function: spec.Name, Reason: "two disks mount at " + d.MountPath}
		case d.SizeBytes%4096 != 0:
			return &InvalidSpecError{Function: spec.Name, Reason: "disk " + d.Name + " size must be whole 4096-byte blocks"}
		}
		seen[d.Name], paths[d.MountPath] = true, true
	}
	if out.Autoscaler != nil && out.Autoscaler.MaxContainers != nil && *out.Autoscaler.MaxContainers > 1 {
		return &InvalidSpecError{Function: spec.Name, Reason: "a workload with a disk runs one container; set max_containers to 1"}
	}
	return nil
}

func allowList(entries *[]string) ([]string, error) {
	out := []string{}
	if entries == nil {
		return out, nil
	}
	if len(*entries) > maxAllowList {
		return nil, fmt.Errorf("allow_list takes at most %d CIDR ranges", maxAllowList)
	}
	for _, entry := range *entries {
		prefix, err := netip.ParsePrefix(entry)
		if err != nil {
			addr, addrErr := netip.ParseAddr(entry)
			if addrErr != nil {
				return nil, fmt.Errorf("allow_list entry %q is not an IP address or CIDR range", entry)
			}
			prefix = netip.PrefixFrom(addr, addr.BitLen())
		}
		out = append(out, prefix.Masked().String())
	}
	return out, nil
}

// firstPort is the port of the first name in order.
func firstPort(ports map[string]int) (int, bool) {
	names := make([]string, 0, len(ports))
	for name := range ports {
		names = append(names, name)
	}
	if len(names) == 0 {
		return 0, false
	}
	slices.Sort(names)
	return ports[names[0]], true
}
