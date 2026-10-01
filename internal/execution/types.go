// Package execution owns task admission, attempts, retries, results,
// cancellation, container lifecycle and the number of containers each release
// needs. Scheduling decides where containers run; compute supplies hosts.
package execution

import (
	"math"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// TaskID identifies a task.
type TaskID uuid.UUID

func (id TaskID) String() string { return uuid.UUID(id).String() }

// AttemptID identifies one attempt of a task. It fences completions.
type AttemptID uuid.UUID

func (id AttemptID) String() string { return uuid.UUID(id).String() }

// ContainerID identifies a container. A container is placed at most once, so
// its id also fences the host it was assigned to.
type ContainerID uuid.UUID

func (id ContainerID) String() string { return uuid.UUID(id).String() }

// TaskStatus is the externally visible task state.
type TaskStatus string

const (
	TaskQueued    TaskStatus = "queued"
	TaskRunning   TaskStatus = "running"
	TaskSucceeded TaskStatus = "succeeded"
	TaskFailed    TaskStatus = "failed"
	TaskCancelled TaskStatus = "cancelled"
)

// Terminal reports whether no further attempt can run.
func (s TaskStatus) Terminal() bool {
	switch s {
	case TaskSucceeded, TaskFailed, TaskCancelled:
		return true
	case TaskQueued, TaskRunning:
		return false
	}
	return false
}

// AttemptState is the outcome of one attempt.
type AttemptState string

const (
	AttemptRunning   AttemptState = "running"
	AttemptSucceeded AttemptState = "succeeded"
	AttemptFailed    AttemptState = "failed"
	AttemptTimedOut  AttemptState = "timed_out"
	AttemptCancelled AttemptState = "cancelled"
	AttemptLost      AttemptState = "lost"
)

// ContainerState is a container's lifecycle position.
type ContainerState string

const (
	// ContainerPending waits for placement.
	ContainerPending ContainerState = "pending"
	// ContainerStarting is assigned to a host that is preparing it.
	ContainerStarting ContainerState = "starting"
	// ContainerReady claims tasks.
	ContainerReady ContainerState = "ready"
	// ContainerDraining claims nothing; its host stops it once running
	// attempts finish.
	ContainerDraining ContainerState = "draining"
	// ContainerStopped is terminal.
	ContainerStopped ContainerState = "stopped"
)

// StopReason records why a container stopped.
type StopReason string

const (
	StopRequested   StopReason = "stopped"
	StopLoadError   StopReason = "load_error"
	StopStartFailed StopReason = "start_failed"
	StopCrashed     StopReason = "crashed"
	StopOutOfMemory StopReason = "out_of_memory"
	StopHostLost    StopReason = "host_lost"
	// StopExited means a pod's command ended on its own.
	StopExited StopReason = "exited"
)

// FailureKind classifies a failed attempt or task.
type FailureKind string

const (
	FailureUserError   FailureKind = "user_error"
	FailureLoadError   FailureKind = "load_error"
	FailureTimeout     FailureKind = "timeout"
	FailureLost        FailureKind = "lost"
	FailureStartFailed FailureKind = "start_failed"
	FailureSystem      FailureKind = "system"
	// FailureDependencyFailed: an upstream task the input refers to did not
	// succeed, or the upstream results were too large to deliver.
	FailureDependencyFailed FailureKind = "dependency_failed"
)

// Retryable reports whether another attempt may follow a failure of kind.
func (k FailureKind) Retryable() bool {
	switch k {
	case FailureUserError, FailureTimeout, FailureLost:
		return true
	case FailureLoadError, FailureStartFailed, FailureSystem, FailureDependencyFailed:
		return false
	}
	return false
}

// Failure is stored in tasks.failure and returned by the API unchanged.
type Failure struct {
	Kind      FailureKind `json:"kind"`
	Type      string      `json:"type,omitempty"`
	Message   string      `json:"message"`
	Traceback string      `json:"traceback,omitempty"`
	Exception []byte      `json:"exception,omitempty"`
}

// Encoding is a payload encoding.
type Encoding string

const (
	EncodingJSON        Encoding = "json"
	EncodingCloudpickle Encoding = "cloudpickle"
)

// MaxPayloadBytes caps one task input or result.
const MaxPayloadBytes = 16 << 20

// RetryPolicy is a release's resolved retry policy.
type RetryPolicy struct {
	MaxAttempts int
	Delay       time.Duration
	Exponential bool
	MaxDelay    time.Duration
}

// RetryPolicyOf resolves spec defaults: without a policy a function gets one
// attempt.
func RetryPolicyOf(spec apitypes.WorkloadSpec) RetryPolicy {
	if spec.RetryPolicy == nil {
		return RetryPolicy{MaxAttempts: 1}
	}
	p := spec.RetryPolicy
	policy := RetryPolicy{MaxAttempts: p.MaxAttempts}
	if p.DelaySeconds != nil {
		policy.Delay = seconds(float64(*p.DelaySeconds))
	}
	if p.Backoff != nil && *p.Backoff == apitypes.Exponential {
		policy.Exponential = true
	}
	if p.MaxDelaySeconds != nil {
		policy.MaxDelay = seconds(float64(*p.MaxDelaySeconds))
	}
	return policy
}

// NextAttemptDelay is the wait before attempt number next (2 or more).
func (p RetryPolicy) NextAttemptDelay(next int) time.Duration {
	delay := p.Delay
	if p.Exponential && next > 2 {
		delay = time.Duration(float64(delay) * math.Pow(2, float64(next-2)))
	}
	if p.MaxDelay > 0 && delay > p.MaxDelay {
		delay = p.MaxDelay
	}
	return delay
}

func seconds(s float64) time.Duration {
	return time.Duration(s * float64(time.Second))
}
