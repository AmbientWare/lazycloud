package storage

import "time"

// SetOrphanAge shortens how old an unowned object must be before the sweep
// removes it, so tests need not wait hours.
func SetOrphanAge(s *Storage, age time.Duration) { s.orphanAge = age }
