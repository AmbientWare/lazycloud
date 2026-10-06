package hostsession

import "time"

// SetLayerLifetime shortens the life of layer grants s issues.
func SetLayerLifetime(s *Server, d time.Duration) { s.layerLifetime = d }

// SetReplicaRecheck shortens how often s checks regional layer copies again.
func SetReplicaRecheck(s *Server, d time.Duration) { s.replicas.recheck = d }
