package main

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// A launch that EC2 never answers holds up no other fleet pass: the
// containers of a host the provider is reclaiming still stop while it
// waits.
func TestALaunchHoldsUpNoPreemption(t *testing.T) {
	pool := dbtest.New(t)
	schedulerEnv(t, pool)
	launching := make(chan struct{}, 1)
	ec2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		if strings.Contains(string(body), "Action=RunInstances") {
			select {
			case launching <- struct{}{}:
			default:
			}
		}
		<-r.Context().Done()
	}))
	for name, value := range map[string]string{
		"LAZYCLOUD_FLEET_NETWORKS":         `{"us-east-2": {"vpc_id": "vpc-1", "security_group_id": "sg-1", "subnets": [{"id": "subnet-1", "zone": "us-east-2a", "zone_id": "use2-az1"}]}}`,
		"LAZYCLOUD_FLEET_IMAGES":           `{"cpu": {"us-east-2": "ami-1"}, "gpu": {"us-east-2": "ami-2"}}`,
		"LAZYCLOUD_FLEET_NODE_ROLE_ARN":    "arn:aws:iam::111122223333:role/node",
		"LAZYCLOUD_FLEET_INSTANCE_PROFILE": "node",
		"LAZYCLOUD_INSTALL_URL":            "https://lazycloud.test",
		"LAZYCLOUD_AGENT_SERVER_ADDR":      "hosts.lazycloud.test:443",
		"LAZYCLOUD_AWS_ENDPOINT_EC2":       ec2.URL,
		"LAZYCLOUD_AWS_ENDPOINT_STS":       ec2.URL,
		"AWS_ENDPOINT_URL":                 ec2.URL,
		"AWS_ACCESS_KEY_ID":                "test",
		"AWS_SECRET_ACCESS_KEY":            "test",
		"AWS_REGION":                       "us-east-2",
	} {
		t.Setenv(name, value)
	}
	exec := func(sql string, args ...any) {
		t.Helper()
		if _, err := pool.Exec(t.Context(), sql, args...); err != nil {
			t.Fatalf("%s: %v", sql, err)
		}
	}
	exec(`insert into agent_releases (version, sha256_amd64, target) values ('1.0.0', repeat('a', 64), true)`)
	exec(`insert into hosts (name, state, kind, provider, phase, cpu_millis, memory_bytes, region, availability_zone, instance_type, market)
values ('launching', 'offline', 'platform', 'aws', 'requested', 2000, 6442450944, 'us-east-2', 'us-east-2a', 'm7i.large', 'on_demand')`)
	var container uuid.UUID
	if err := pool.QueryRow(t.Context(), `
with reclaimed as (
    insert into hosts (name, token_hash, state, last_seen_at, kind, provider, phase, capacity_state, interruption_at, cpu_millis, memory_bytes)
    values ('reclaimed', sha256('t'), 'online', now(), 'platform', 'aws', 'draining', 'preempting', now(), 2000, 6442450944) returning id
), ws as (insert into workspaces (name) values ('ws') returning id),
   app as (insert into apps (workspace_id, name, state) select id, 'app', 'active' from ws returning id),
   wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id),
   rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
           select id, 1, '{}', sha256('spec'), sha256('src') from wl returning id, workload_id)
insert into containers (workspace_id, release_id, workload_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at)
select ws.id, rel.id, rel.workload_id, 'ready', reclaimed.id, 1, 1000, 1 << 30, now(), now() from ws, rel, reclaimed
returning id`).Scan(&container); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- run(ctx, slog.New(slog.DiscardHandler)) }()
	defer func() {
		cancel()
		<-done
		ec2.Close()
	}()
	select {
	case <-launching:
	case <-time.After(30 * time.Second):
		t.Fatal("no launch reached EC2")
	}
	for deadline := time.Now().Add(15 * time.Second); ; time.Sleep(200 * time.Millisecond) {
		var state string
		if err := pool.QueryRow(t.Context(), "select state from containers where id = $1", container).Scan(&state); err != nil {
			t.Fatal(err)
		}
		if state == "stopped" {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("the reclaimed host's container is %s while a launch waits on EC2, want stopped", state)
		}
	}
}
