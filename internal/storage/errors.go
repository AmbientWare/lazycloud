package storage

import (
	"fmt"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// ErrNotFound means the volume, file, disk, artifact or map entry does not
// exist in the workspace.
var ErrNotFound error = &apitypes.Error{Code: apitypes.NotFound, Message: "not found"}

// ErrTooLarge means a queue message or map value exceeds its limit.
var ErrTooLarge error = &apitypes.Error{Code: apitypes.PayloadTooLarge, Message: "a value is larger than 1 MiB"}

func invalid(format string, args ...any) error {
	return &apitypes.Error{Code: apitypes.InvalidRequest, Message: fmt.Sprintf(format, args...)}
}

func conflict(format string, args ...any) error {
	return &apitypes.Error{Code: apitypes.Conflict, Message: fmt.Sprintf(format, args...)}
}

// optionalTime reads a nullable aggregate that sqlc types as any.
func optionalTime(v any) *time.Time {
	if t, ok := v.(time.Time); ok {
		return &t
	}
	return nil
}
