package control

import (
	"errors"
	"net/url"
	"strings"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
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
			return &InvalidSpecError{Function: spec.Name, Reason: err.Error()}
		}
		normalized := cron.String()
		out.Cron = &normalized
	}
	warmFloor := out.Autoscaler != nil && out.Autoscaler.MinContainers != nil && *out.Autoscaler.MinContainers > 0
	// A pod's keep-warm, -1 included, is resolvePod's.
	if spec.KeepWarmSeconds != nil && *spec.KeepWarmSeconds < 0 && !warmFloor && spec.Pod == nil {
		return &InvalidSpecError{Function: spec.Name, Reason: "keep_warm=-1 is only supported for functions with a warm floor (autoscaler.min_containers above zero)"}
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
		normalized, err := callbackURL(*spec.CallbackUrl)
		if err != nil {
			return &InvalidSpecError{Function: spec.Name, Reason: err.Error()}
		}
		out.CallbackUrl = normalized
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

// callbackURL checks a callback target: http or https,
// a host, no credentials and no fragment. A blank value means none.
func callbackURL(value string) (*string, error) {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return nil, nil //nolint:nilnil // A blank URL configures no callback.
	}
	parsed, err := url.Parse(trimmed)
	if err != nil {
		return nil, errors.New("callback_url is not a URL")
	}
	switch {
	case parsed.Scheme != "http" && parsed.Scheme != "https":
		return nil, errors.New("callback_url must use http or https")
	case parsed.Hostname() == "":
		return nil, errors.New("callback_url must include a hostname")
	case parsed.User != nil:
		return nil, errors.New("callback_url must not contain credentials")
	case parsed.Fragment != "" || strings.Contains(trimmed, "#"):
		return nil, errors.New("callback_url must not contain a fragment")
	}
	return &trimmed, nil
}
