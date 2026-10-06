package images

import "time"

// SetPlatformLease shortens the lease of i's platform image conversions.
func SetPlatformLease(i *Images, d time.Duration) { i.platformLease = d }
