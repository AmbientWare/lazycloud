package images

import (
	"context"
	"time"
)

// ConvertPlatformImageLeased is ConvertPlatformImage with a lease of lease.
func ConvertPlatformImageLeased(ctx context.Context, i *Images, reference, architecture string, lease time.Duration) error {
	return i.convertPlatformImage(ctx, reference, architecture, lease)
}
