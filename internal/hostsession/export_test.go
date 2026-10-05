package hostsession

import "time"

// SetLayerLifetime shortens the life of layer grants s issues.
func SetLayerLifetime(s *Server, d time.Duration) { s.layerLifetime = d }
