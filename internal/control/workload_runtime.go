package control

import (
	"strings"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/callbacks"
	"github.com/AmbientWare/lazycloud/internal/schedules"
)

// Keep-warm defaults: a scheduled function stops as soon as it is idle, and
// a function with a warm minimum leaves retirement to the planner, which
// stops idle containers above the minimum at once.
const (
	defaultKeepWarmSeconds   = 10
	scheduledKeepWarmSeconds = 0
	plannerKeepWarmSeconds   = -1
)

// resolveRuntime fills the workload runtime options of out, resolved from
// spec: the normalized cron, the keep-warm default it implies, the callback
// URL, in_process and the lifecycle hooks.
func resolveRuntime(spec apitypes.WorkloadSpec, out *apitypes.WorkloadSpec) error {
	if spec.Cron != nil {
		cron, err := schedules.ParseCron(*spec.Cron)
		if err != nil {
			return invalidSpec(spec.Name, err.Error())
		}
		normalized := cron.String()
		out.Cron = &normalized
	}
	warmFloor := out.Autoscaler != nil && out.Autoscaler.MinContainers != nil && *out.Autoscaler.MinContainers > 0
	// A pod's keep-warm, -1 included, is resolvePod's.
	if spec.KeepWarmSeconds != nil && *spec.KeepWarmSeconds < 0 && !warmFloor && spec.Pod == nil {
		return invalidSpec(spec.Name, "keep_warm=-1 is only supported for functions with a warm floor (autoscaler.min_containers above zero)")
	}
	keepWarm := defaultKeepWarmSeconds
	switch {
	case warmFloor:
		keepWarm = plannerKeepWarmSeconds
	case spec.KeepWarmSeconds != nil:
		keepWarm = *spec.KeepWarmSeconds
	case spec.Cron != nil:
		keepWarm = scheduledKeepWarmSeconds
	}
	out.KeepWarmSeconds = &keepWarm

	if spec.CallbackUrl != nil {
		// A blank callback_url configures no callback.
		out.CallbackUrl = nil
		if strings.TrimSpace(*spec.CallbackUrl) != "" {
			target, err := callbacks.TargetURL(*spec.CallbackUrl)
			if err != nil {
				return invalidSpec(spec.Name, "callback_url "+err.Error())
			}
			out.CallbackUrl = &target
		}
	}
	out.InProcess = orDefault(spec.InProcess, false)
	secrets := []string{}
	if spec.Secrets != nil {
		secrets = append(secrets, *spec.Secrets...)
	}
	out.Secrets = &secrets
	hooks := apitypes.LifecycleHooks{}
	if spec.LifecycleHooks != nil {
		hooks = *spec.LifecycleHooks
	}
	for _, refs := range []**apitypes.HookReferences{
		&hooks.OnStart, &hooks.OnRunning, &hooks.OnSuccess, &hooks.OnError, &hooks.OnRetry, &hooks.OnFailure, &hooks.OnFinish,
	} {
		*refs = orDefault(*refs, apitypes.HookReferences{})
	}
	out.LifecycleHooks = &hooks
	return nil
}
