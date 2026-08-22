# Redis leaves the cluster.
#
# It holds the leases the scheduler serialises capacity work on, the container
# runtime state, and the control plane's published origin. On the previous
# deployment it was a container on the one host, which is what made a second host
# impossible: two hosts with two Redises is two schedulers that do not know about
# each other. Managed, it is one Redis several replicas share, and running more
# than one of anything becomes a capacity decision rather than a correctness one.
#
# Single node. Replication here buys failover for state that is rebuilt on
# reconnect -- leases expire, origins are republished -- and costs twice the
# instance to protect it.

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${var.deployment}-redis"
  subnet_ids = aws_subnet.cluster[*].id
}

resource "aws_security_group" "redis" {
  name        = "${var.deployment}-redis"
  description = "Redis: reachable only from inside this VPC."
  vpc_id      = aws_vpc.cluster.id

  tags = { Name = "${var.deployment}-redis" }
}

# The VPC rather than the node security group, because Auto Mode owns the
# security group its nodes carry and naming it here would be naming something
# this module does not create.
resource "aws_vpc_security_group_ingress_rule" "redis" {
  security_group_id = aws_security_group.redis.id
  description       = "Cluster workloads."
  cidr_ipv4         = aws_vpc.cluster.cidr_block
  from_port         = 6379
  to_port           = 6379
  ip_protocol       = "tcp"
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id = "${var.deployment}-redis"
  description          = "LazyCloud coordination: leases, runtime state, published origins."

  engine         = "redis"
  engine_version = var.redis_engine_version
  node_type      = var.redis_node_type
  port           = 6379

  num_cache_clusters         = 1
  automatic_failover_enabled = false

  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [aws_security_group.redis.id]

  # In transit but not at rest: the data is coordination state with a lifetime
  # measured in seconds, and encryption at rest here would be protecting a disk
  # nothing durable is written to. TLS matters because the traffic crosses the
  # VPC.
  transit_encryption_enabled = true
  at_rest_encryption_enabled = false

  # No auth token. The endpoint is reachable only from inside this VPC and the
  # security group says so; a token stored in Secrets Manager to reach a service
  # already closed to everything else is one more credential to rotate.
  apply_immediately = true

  tags = { Name = "${var.deployment}-redis" }
}
