package storage

import (
	"errors"
	"fmt"
	"time"
)

// ErrNotFound means the volume, file, disk, artifact or map entry does not
// exist in the workspace.
var ErrNotFound = errors.New("not found")

// ErrTooLarge means a queue message or map value exceeds its limit.
var ErrTooLarge = errors.New("value too large")

// InvalidError rejects a request the schema cannot express, such as a path
// that leaves the volume.
type InvalidError struct{ Reason string }

func (e *InvalidError) Error() string { return e.Reason }

func invalid(format string, args ...any) error {
	return &InvalidError{Reason: fmt.Sprintf(format, args...)}
}

// ConflictError means the request conflicts with current state: a volume or
// disk in use, a move onto an existing path, or a stale map revision.
type ConflictError struct{ Reason string }

func (e *ConflictError) Error() string { return e.Reason }

func conflict(format string, args ...any) error {
	return &ConflictError{Reason: fmt.Sprintf(format, args...)}
}

// optionalTime reads a nullable aggregate that sqlc types as any.
func optionalTime(v any) *time.Time {
	if t, ok := v.(time.Time); ok {
		return &t
	}
	return nil
}
