package observability_test

import (
	"testing"
	"time"

	"github.com/google/uuid"
)

// Buckets count every workspace's work, each in the UTC day it began; work
// before the range counts nowhere.
func TestPlatformMetricsCountDailyActivityAcrossWorkspaces(t *testing.T) {
	t.Parallel()
	f := newFixture(t, `{}`)
	otherWS, _, otherWL, otherRel := f.addFunction("shared", "etl", "load", `{}`)
	today := time.Now().UTC().Truncate(24 * time.Hour)
	yesterday := today.Add(-24 * time.Hour)

	f.task(f.workspace, f.workload, f.release, "succeeded", yesterday.Add(time.Hour), 3*time.Second, time.Second)
	f.task(f.workspace, f.workload, f.release, "failed", yesterday.Add(time.Hour), 3*time.Second, time.Second)
	f.task(otherWS, otherWL, otherRel, "succeeded", today, 0, time.Second)
	f.task(f.workspace, f.workload, f.release, "succeeded", today.Add(-72*time.Hour), 0, time.Second)
	for _, status := range []int{200, 503} {
		f.exec1(`insert into http_requests (id, workspace_id, workload_id, release_id, method, path, status, started_at, duration_ms, request_bytes, response_bytes)
values (uuidv7($4::timestamptz - now()), $1, $2, $3, 'GET', '/', $5, $4, 1, 0, 0)`,
			uuid.UUID(otherWS), otherWL, otherRel, yesterday.Add(2*time.Hour), status)
	}
	created := yesterday.Add(3 * time.Hour)
	stopped := created.Add(time.Minute)
	f.container(f.release, "stopped", created, &created, &stopped, 1000)
	f.container(f.release, "ready", created, &created, nil, 1000)
	f.exec1(`update containers set ready_at = created_at + interval '2 seconds' where state = 'ready'`)
	before, err := f.obs.PlatformMetrics(t.Context(), 24*time.Hour, 2)
	if err != nil {
		t.Fatal(err)
	}
	f.exec1(`with u as (insert into users (email, created_at) values ('a@example.com', $1), ('b@example.com', $1 - interval '90 days') returning id)
insert into sign_ins (user_id, at) select id, $1 from u union all select id, $1 + interval '1 hour' from u limit 3`, yesterday)

	m, err := f.obs.PlatformMetrics(t.Context(), 24*time.Hour, 2)
	if err != nil {
		t.Fatal(err)
	}
	if m.Users != before.Users+2 || m.Workspaces != 2 || m.Apps != 2 || m.LiveContainers != 1 || m.SignedIn != 2 || len(m.Buckets) != 2 {
		t.Fatalf("totals %+v", m)
	}
	y, d := m.Buckets[0], m.Buckets[1]
	if !y.Timestamp.Equal(yesterday) || y.SignUps != 1 || y.SignIns != 2 || y.ActiveWorkspaces != 2 ||
		y.Tasks != 2 || y.FailedTasks != 1 || y.Requests != 2 || y.FailedRequests != 1 ||
		y.ContainerStarts != 2 || y.FailedStarts != 1 || y.StartP50Ms == nil || *y.StartP50Ms != 2000 || y.QueueP50Ms == nil || *y.QueueP50Ms != 3000 {
		t.Fatalf("yesterday %+v", y)
	}
	// The fixture deployed its two releases as the test began.
	if d.Tasks != 1 || d.ActiveWorkspaces != 1 || d.Deployments != 2 || d.StartP50Ms != nil {
		t.Fatalf("today %+v", d)
	}
}
