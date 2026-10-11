package diskengine

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	exportID   = "disk"
	exportName = "disk"
	// qmpTimeout bounds one monitor command when ctx sets no earlier deadline.
	qmpTimeout = 60 * time.Second
	// daemonStopTimeout bounds waiting for a daemon to exit after quit.
	daemonStopTimeout = 30 * time.Second
)

// qmpClient speaks to one daemon's monitor. The monitor serves one client at
// a time; the disk lock keeps callers from competing for it.
type qmpClient struct {
	conn    net.Conn
	scanner *bufio.Scanner
}

type qmpError struct {
	Class string `json:"class"`
	Desc  string `json:"desc"`
}

func dialQMP(ctx context.Context, socket string) (*qmpClient, error) {
	var dialer net.Dialer
	conn, err := dialer.DialContext(ctx, "unix", socket)
	if err != nil {
		return nil, fmt.Errorf("connect to qemu-storage-daemon monitor %s: %w", socket, err)
	}
	scanner := bufio.NewScanner(conn)
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	client := &qmpClient{conn: conn, scanner: scanner}
	if err := client.roundTrip(ctx, "greeting", nil, nil); err != nil {
		return nil, errors.Join(err, client.close())
	}
	if err := client.execute(ctx, "qmp_capabilities", nil, nil); err != nil {
		return nil, errors.Join(err, client.close())
	}
	return client, nil
}

func (c *qmpClient) close() error {
	if err := c.conn.Close(); err != nil {
		return fmt.Errorf("close monitor connection: %w", err)
	}
	return nil
}

func (c *qmpClient) execute(ctx context.Context, name string, args, out any) error {
	request := map[string]any{"execute": name}
	if args != nil {
		request["arguments"] = args
	}
	payload, err := json.Marshal(request)
	if err != nil {
		return fmt.Errorf("monitor %s: encode: %w", name, err)
	}
	return c.roundTrip(ctx, name, append(payload, '\n'), out)
}

// roundTrip writes payload, when there is one, and reads the reply, skipping
// events. ctx's end interrupts the connection's reads and writes.
func (c *qmpClient) roundTrip(ctx context.Context, name string, payload []byte, out any) error {
	deadline := time.Now().Add(qmpTimeout)
	if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
		deadline = d
	}
	if err := c.conn.SetDeadline(deadline); err != nil {
		return fmt.Errorf("monitor %s: %w", name, err)
	}
	stop := context.AfterFunc(ctx, func() { _ = c.conn.SetDeadline(time.Unix(1, 0)) })
	defer stop()
	fail := func(err error) error {
		if ctx.Err() != nil {
			return fmt.Errorf("monitor %s: %w", name, ctx.Err())
		}
		return fmt.Errorf("monitor %s: %w", name, err)
	}
	if payload != nil {
		if _, err := c.conn.Write(payload); err != nil {
			return fail(err)
		}
	}
	for {
		if !c.scanner.Scan() {
			if err := c.scanner.Err(); err != nil {
				return fail(err)
			}
			return fail(errors.New("monitor closed the connection"))
		}
		var message map[string]json.RawMessage
		if err := json.Unmarshal(c.scanner.Bytes(), &message); err != nil {
			return fail(fmt.Errorf("parse monitor message: %w", err))
		}
		if payload == nil {
			return nil // The greeting.
		}
		if _, isEvent := message["event"]; isEvent {
			continue
		}
		if raw, failed := message["error"]; failed {
			var qerr qmpError
			if err := json.Unmarshal(raw, &qerr); err != nil {
				return fail(fmt.Errorf("parse error reply: %w", err))
			}
			return fmt.Errorf("monitor %s: %s: %s", name, qerr.Class, qerr.Desc)
		}
		raw, ok := message["return"]
		if !ok {
			return fail(fmt.Errorf("unexpected reply %s", c.scanner.Bytes()))
		}
		if out != nil {
			if err := json.Unmarshal(raw, out); err != nil {
				return fail(fmt.Errorf("parse reply: %w", err))
			}
		}
		return nil
	}
}

// baseBlockdev is the read-only node of the served generation's file.
func baseBlockdev(generation int64, path string) map[string]any {
	return map[string]any{
		"driver": "raw", "node-name": baseNode(generation), "read-only": true,
		"file": map[string]any{"driver": "file", "filename": path, "node-name": baseNode(generation) + "-file", "read-only": true},
	}
}

// layerBlockdev is layer l of the stack over the node below, or none, as
// blockdev-add takes it.
func layerBlockdev(p diskPaths, l layer, below any, head bool) map[string]any {
	node := map[string]any{
		"driver":    "qcow2",
		"node-name": l.node(),
		"file":      map[string]any{"driver": "file", "filename": p.layerPath(l), "node-name": l.fileNode()},
		"backing":   below,
	}
	if head {
		node["discard"] = "unmap"
	} else {
		node["read-only"] = true
	}
	return node
}

// stackBlockdevs describes the stack as one blockdev per node, base first,
// each naming the node below by name, so seal and rebase address layers
// directly.
func stackBlockdevs(p diskPaths, state *diskState, basePath string) []map[string]any {
	var nodes []map[string]any
	var below any // JSON null: the bottom layer of a disk never published
	if state.Base != nil {
		nodes = append(nodes, baseBlockdev(state.Base.Generation, basePath))
		below = baseNode(state.Base.Generation)
	}
	for i, l := range state.Layers {
		nodes = append(nodes, layerBlockdev(p, l, below, i == len(state.Layers)-1))
		below = l.node()
	}
	return nodes
}

// startDaemon starts the disk's qemu-storage-daemon in a transient systemd
// scope, outside the caller's cgroup, so stopping or restarting the caller's
// service leaves the disk served. The daemon detaches once its sockets
// listen and exports the head over NBD on the disk's unix socket. A caller
// other than root gets a scope of its user manager.
func startDaemon(ctx context.Context, p diskPaths, state *diskState, basePath string) (int, error) {
	if err := p.checkSocketPaths(); err != nil {
		return 0, err
	}
	if err := os.MkdirAll(p.runDir(), 0o700); err != nil {
		return 0, fmt.Errorf("create run directory: %w", err)
	}
	for _, stale := range []string{p.qmpSocket(), p.nbdSocket(), p.pidFile()} {
		if err := removeIfExists(stale); err != nil {
			return 0, err
		}
	}
	// Scope names are unique per start: systemd forgets a scope only some
	// time after its last process exits.
	args := []string{"--scope", "--collect", "--quiet", fmt.Sprintf("--unit=lazycloud-disk-%s-%d", p.id, time.Now().UnixNano())}
	if os.Geteuid() != 0 {
		args = append([]string{"--user"}, args...)
	}
	args = append(args, "--", toolDaemon,
		"--daemonize",
		"--pidfile", p.pidFile(),
		"--chardev", "socket,id=monitor,path="+p.qmpSocket()+",server=on,wait=off",
		"--monitor", "chardev=monitor",
	)
	for _, node := range stackBlockdevs(p, state, basePath) {
		spec, err := json.Marshal(node)
		if err != nil {
			return 0, fmt.Errorf("encode blockdev: %w", err)
		}
		args = append(args, "--blockdev", string(spec))
	}
	args = append(args,
		"--nbd-server", "addr.type=unix,addr.path="+p.nbdSocket(),
		"--export", "type=nbd,id="+exportID+",node-name="+state.head().node()+",name="+exportName+",writable=on",
	)
	if _, err := runTool(ctx, toolRunUnit, args...); err != nil {
		return 0, err
	}
	pid, err := readPID(p.pidFile())
	if err != nil {
		return 0, fmt.Errorf("qemu-storage-daemon started without a pid file: %w", err)
	}
	return pid, nil
}

// stopUnrecordedDaemon stops a daemon of this disk that an attach
// interrupted between starting it and recording it.
func stopUnrecordedDaemon(ctx context.Context, p diskPaths) error {
	pid, err := readPID(p.pidFile())
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	return stopDaemon(ctx, p, pid)
}

func readPID(path string) (int, error) {
	raw, err := os.ReadFile(path) //nolint:gosec // A pid file under the root.
	if err != nil {
		return 0, fmt.Errorf("read %s: %w", path, err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil {
		return 0, fmt.Errorf("parse %s: %w", path, err)
	}
	return pid, nil
}

// daemonAlive reports whether pid is this disk's daemon. After a restart the
// kernel may have handed the pid to another process, so its command line must
// name this disk's monitor socket.
func daemonAlive(p diskPaths, pid int) bool {
	return slices.ContainsFunc(processArgs(pid), func(arg string) bool { return strings.Contains(arg, p.qmpSocket()) })
}

func stopDaemon(ctx context.Context, p diskPaths, pid int) error {
	if !daemonAlive(p, pid) {
		return nil
	}
	quitErr := func() error {
		client, err := dialQMP(ctx, p.qmpSocket())
		if err != nil {
			return err
		}
		return errors.Join(client.execute(ctx, "quit", nil, nil), client.close())
	}()
	if quitErr != nil {
		if err := syscall.Kill(pid, syscall.SIGTERM); err != nil && !errors.Is(err, syscall.ESRCH) {
			return fmt.Errorf("stop qemu-storage-daemon %d: quit failed (%w) and SIGTERM failed: %w", pid, quitErr, err)
		}
	}
	deadline := time.Now().Add(daemonStopTimeout)
	for daemonAlive(p, pid) {
		if time.Now().After(deadline) {
			return fmt.Errorf("qemu-storage-daemon %d did not exit within %s", pid, daemonStopTimeout)
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("wait for qemu-storage-daemon %d to exit: %w", pid, ctx.Err())
		case <-time.After(50 * time.Millisecond):
		}
	}
	return nil
}

// headWritten reports whether the head took a write past the superblock since
// the daemon opened it. Every thaw rewrites the superblock, so each new head
// receives that one block within milliseconds of a seal; counting it would
// seal a layer every cycle on an idle disk. Any change a workload makes
// writes inodes, bitmaps or data beyond block 0. The export's own backend is
// anonymous and absent from the statistics, so this reads the node's
// high-water mark.
func headWritten(ctx context.Context, client *qmpClient, node string) (bool, error) {
	var stats []struct {
		NodeName string `json:"node-name"`
		Stats    struct {
			WrHighestOffset int64 `json:"wr_highest_offset"`
		} `json:"stats"`
	}
	if err := client.execute(ctx, "query-blockstats", map[string]any{"query-nodes": true}, &stats); err != nil {
		return false, err
	}
	for _, entry := range stats {
		if entry.NodeName == node {
			// Block 0 holds the superblock and nothing else a workload writes.
			return entry.Stats.WrHighestOffset > hostproto.DiskBlockBytes, nil
		}
	}
	return false, fmt.Errorf("qemu-storage-daemon has no node %s", node)
}

func exportedNode(ctx context.Context, client *qmpClient) (string, error) {
	var exports []struct {
		ID       string `json:"id"`
		NodeName string `json:"node-name"`
	}
	if err := client.execute(ctx, "query-block-exports", nil, &exports); err != nil {
		return "", err
	}
	for _, export := range exports {
		if export.ID == exportID {
			return export.NodeName, nil
		}
	}
	return "", errors.New("qemu-storage-daemon has no disk export")
}

// reconcileHead makes the recorded head match the node the daemon exports.
// They differ only when a seal stopped between recording a new head and
// switching to it.
func reconcileHead(ctx context.Context, p diskPaths, state *diskState, client *qmpClient) error {
	exported, err := exportedNode(ctx, client)
	if err != nil {
		return err
	}
	head := state.head()
	if exported == head.node() {
		return nil
	}
	if len(state.Layers) < 2 || exported != state.Layers[len(state.Layers)-2].node() {
		return fmt.Errorf("qemu-storage-daemon exports %s but disk %s records head %s", exported, p.id, head.node())
	}
	state.Layers = state.Layers[:len(state.Layers)-1]
	state.HeadFresh = false
	if err := saveState(p, state); err != nil {
		return err
	}
	return removeIfExists(p.layerPath(head))
}

func daemonHeadWritten(ctx context.Context, p diskPaths, state *diskState) (written bool, err error) {
	err = withMonitor(ctx, p, state, func(client *qmpClient) error {
		written, err = headWritten(ctx, client, state.head().node())
		return err
	})
	return written, err
}

// withMonitor runs fn on a connection to the disk's daemon monitor once the
// recorded head is the one the daemon exports.
func withMonitor(ctx context.Context, p diskPaths, state *diskState, fn func(*qmpClient) error) error {
	client, err := dialQMP(ctx, p.qmpSocket())
	if err != nil {
		return err
	}
	if err = reconcileHead(ctx, p, state, client); err == nil {
		err = fn(client)
	}
	return errors.Join(err, client.close())
}

// namedNodes lists the nodes the daemon holds by name.
func namedNodes(ctx context.Context, client *qmpClient) (map[string]bool, error) {
	var nodes []struct {
		NodeName string `json:"node-name"`
	}
	if err := client.execute(ctx, "query-named-block-nodes", map[string]any{"flat": true}, &nodes); err != nil {
		return nil, err
	}
	names := make(map[string]bool, len(nodes))
	for _, n := range nodes {
		names[n.NodeName] = true
	}
	return names, nil
}

// rebase moves the running stack onto the pending generation's file at
// path: the lowest layer the generation does not hold takes its node as
// backing, and the nodes of the layers it holds and of the old base go,
// newest first. blockdev-reopen drains that one layer, so writes pause for
// the switch alone. A rebase interrupted part way finishes when run again.
func rebase(ctx context.Context, p diskPaths, state *diskState, path string) error {
	pending := state.Pending
	return withMonitor(ctx, p, state, func(client *qmpClient) error {
		names, err := namedNodes(ctx, client)
		if err != nil {
			return err
		}
		next := baseNode(pending.Generation)
		if !names[next] {
			if err := client.execute(ctx, "blockdev-add", baseBlockdev(pending.Generation, path), nil); err != nil {
				return err
			}
		}
		kept := slices.IndexFunc(state.Layers, func(l layer) bool { return l.Seq > pending.Through })
		above := layerBlockdev(p, state.Layers[kept], next, kept == len(state.Layers)-1)
		above["file"] = state.Layers[kept].fileNode()
		if err := client.execute(ctx, "blockdev-reopen", map[string]any{"options": []any{above}}, nil); err != nil {
			return fmt.Errorf("move disk %s onto generation %d: %w", p.id, pending.Generation, err)
		}
		gone := make([]string, 0, kept+1)
		for _, l := range slices.Backward(state.Layers[:kept]) {
			gone = append(gone, l.node())
		}
		if state.Base != nil {
			gone = append(gone, baseNode(state.Base.Generation))
		}
		for _, name := range gone {
			if names[name] {
				if err := client.execute(ctx, "blockdev-del", map[string]any{"node-name": name}, nil); err != nil {
					return fmt.Errorf("release node %s: %w", name, err)
				}
			}
		}
		return nil
	})
}
