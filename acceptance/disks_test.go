package acceptance

import (
	"encoding/json"
	"net/http"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/diskengine"
)

// A devbox's writes outlive its container: written on one host, stopped,
// and started on another host whose engine holds nothing of the disk, its
// root disk reads back the same bytes. It needs root and the nbd module, as
// hosts run the disk engine.
func TestDevboxDiskMovesBetweenHosts(t *testing.T) {
	if err := diskengine.Check(); err != nil {
		t.Skip(err)
	}
	p := startPlatform(t)
	ctx := t.Context()
	var err error
	if p.join, _, err = p.compute.CreateJoinToken(ctx, time.Hour); err != nil {
		t.Fatal(err)
	}
	p.runAgent()
	for deadline := time.Now().Add(time.Minute); ; time.Sleep(100 * time.Millisecond) {
		var online int
		if err := p.pool.QueryRow(ctx, "select count(*) from hosts where state = 'online'").Scan(&online); err != nil {
			t.Fatal(err)
		}
		if online == 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the second host did not join within a minute")
		}
	}
	box := spec("box", "", p.upload(map[string]string{"app.py": ""}), nil)
	box.Kind, box.Handler = apitypes.WorkloadKindPod, nil
	box.Pod = &apitypes.PodSpec{Kind: apitypes.PodKindDevbox}
	box.Disks = &[]apitypes.DiskMountSpec{{Name: "box", SizeBytes: 10 << 30, MountPath: "/"}}
	p.deploy("dev", box)

	first, firstHost := p.readyDevbox(uuid.Nil)
	written := p.shell(first, "head -c 33554432 /dev/urandom > /root/moved && sha256sum /root/moved")
	if status := p.apiCall(http.MethodPost, "/v1/workspaces/ws/apps/dev/workloads/pod/box/devbox/stop", nil, nil); status != http.StatusOK {
		t.Fatalf("stop the devbox: %d", status)
	}
	for deadline := time.Now().Add(5 * time.Minute); ; time.Sleep(time.Second) {
		var released bool
		if err := p.pool.QueryRow(ctx, "select released_at is not null from disks where name = 'box'").Scan(&released); err != nil {
			t.Fatal(err)
		}
		if released {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the stopped devbox did not save its disk within five minutes")
		}
	}
	if _, err := p.pool.Exec(ctx, "update hosts set capacity_state = 'cordoned' where id = $1", firstHost); err != nil {
		t.Fatal(err)
	}
	if status := p.apiCall(http.MethodPost, "/v1/workspaces/ws/apps/dev/workloads/pod/box/devbox/start", nil, nil); status != http.StatusOK {
		t.Fatalf("start the devbox: %d", status)
	}
	second, _ := p.readyDevbox(firstHost)
	if read := p.shell(second, "sha256sum /root/moved"); read != written {
		t.Fatalf("the moved disk holds %s, written %s", read, written)
	}
}

// readyDevbox waits for the devbox's container to be ready on a host other
// than not and returns it and its host.
func (p *platform) readyDevbox(not uuid.UUID) (uuid.UUID, uuid.UUID) {
	p.t.Helper()
	for deadline := time.Now().Add(5 * time.Minute); ; time.Sleep(time.Second) {
		var container, host uuid.UUID
		err := p.pool.QueryRow(p.t.Context(), `
select c.id, c.host_id from containers c join releases r on r.id = c.release_id join workloads w on w.id = r.workload_id
where w.name = 'box' and c.state = 'ready' and c.host_id <> $1`, not).Scan(&container, &host)
		if err == nil {
			return container, host
		}
		if time.Now().After(deadline) {
			p.t.Fatalf("the devbox was not ready within five minutes: %v", err)
		}
	}
}

var sha256Line = regexp.MustCompile(`\b([0-9a-f]{64})\s+/root/moved`)

// shell runs command in a login shell of container and returns the
// sha256sum it prints.
func (p *platform) shell(container uuid.UUID, command string) string {
	p.t.Helper()
	ctx := p.t.Context()
	url := strings.Replace(p.api, "http", "ws", 1) + "/v1/workspaces/ws/containers/" + container.String() + "/shell"
	conn, resp, err := websocket.Dial(ctx, url, &websocket.DialOptions{HTTPHeader: http.Header{"Authorization": {"Bearer " + p.token}}})
	if resp != nil && resp.Body != nil {
		_ = resp.Body.Close()
	}
	if err != nil {
		p.t.Fatalf("open a shell: %v", err)
	}
	defer func() { _ = conn.CloseNow() }()
	conn.SetReadLimit(1 << 20)
	if err := conn.Write(ctx, websocket.MessageBinary, []byte(command+"; exit\n")); err != nil {
		p.t.Fatal(err)
	}
	var out strings.Builder
	for {
		kind, data, err := conn.Read(ctx)
		if err != nil {
			p.t.Fatalf("the shell ended without exiting: %v; it printed %q", err, out.String())
		}
		if kind == websocket.MessageBinary {
			out.Write(data)
			continue
		}
		var msg struct {
			Type    string `json:"type"`
			Code    int    `json:"code"`
			Message string `json:"message"`
		}
		if err := json.Unmarshal(data, &msg); err != nil {
			p.t.Fatal(err)
		}
		if msg.Type == "error" || msg.Type == "exit" && msg.Code != 0 {
			p.t.Fatalf("the shell failed: %+v; it printed %q", msg, out.String())
		}
		if msg.Type == "exit" {
			break
		}
	}
	m := sha256Line.FindStringSubmatch(out.String())
	if m == nil {
		p.t.Fatalf("the shell printed no checksum: %q", out.String())
	}
	return m[1]
}
