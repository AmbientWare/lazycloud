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
)

const (
	exportID   = "disk"
	exportName = "disk"
	// qmpTimeout bounds one monitor command when ctx sets no earlier deadline.
	qmpTimeout = 60 * time.Second
	// daemonStopTimeout bounds waiting for a daemon to exit after quit.
	daemonStopTimeout = 30 * time.Second
	// filesystemBlockBytes is the ext4 block size formatExt4 pins. Block 0
	// holds the superblock and nothing else a workload writes.
	filesystemBlockBytes = 4096
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

func fileChild(path string) map[string]any {
	return map[string]any{"driver": "file", "filename": path}
}

// layerNodes describes the chain as one blockdev per layer, each naming the
// layer below by node name, so seal and compact can address layers directly.
func layerNodes(p diskPaths, state *diskState) []map[string]any {
	head := len(state.Layers) - 1
	// Compaction commits zeroes a discard left in the head into the base;
	// detecting them there frees its space instead of writing them.
	compacts := head > 0
	nodes := make([]map[string]any, 0, len(state.Layers))
	for i, l := range state.Layers {
		node := map[string]any{
			"driver":    string(l.format()),
			"node-name": l.node(),
			"file":      fileChild(p.layerPath(l)),
		}
		if i < head {
			// Sealed; a commit into it reopens it for writing.
			node["read-only"] = true
			node["auto-read-only"] = true
		}
		switch {
		case i > 0:
			node["backing"] = state.Layers[i-1].node()
		case !l.Raw:
			// A raw base takes no backing option at all.
			node["backing"] = nil
		}
		if i == head || (compacts && i == 0) {
			node["discard"] = "unmap"
		}
		if compacts && i == 0 {
			node["detect-zeroes"] = "unmap"
		}
		nodes = append(nodes, node)
	}
	return nodes
}

// startDaemon starts the disk's qemu-storage-daemon, which detaches and
// keeps serving after this call returns, and exports the head over NBD on
// the disk's unix socket.
func startDaemon(ctx context.Context, p diskPaths, state *diskState) (int, error) {
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
	args := []string{
		"--daemonize",
		"--pidfile", p.pidFile(),
		"--chardev", "socket,id=monitor,path=" + p.qmpSocket() + ",server=on,wait=off",
		"--monitor", "chardev=monitor",
	}
	for _, node := range layerNodes(p, state) {
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
	if _, err := runTool(ctx, toolDaemon, args...); err != nil {
		return 0, err
	}
	raw, err := os.ReadFile(p.pidFile())
	if err != nil {
		return 0, fmt.Errorf("qemu-storage-daemon started without a pid file: %w", err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil {
		return 0, fmt.Errorf("parse %s: %w", p.pidFile(), err)
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
			return entry.Stats.WrHighestOffset > filesystemBlockBytes, nil
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

func daemonHeadWritten(ctx context.Context, p diskPaths, state *diskState) (bool, error) {
	client, err := dialQMP(ctx, p.qmpSocket())
	if err != nil {
		return false, err
	}
	written, err := func() (bool, error) {
		if err := reconcileHead(ctx, p, state, client); err != nil {
			return false, err
		}
		return headWritten(ctx, client, state.head().node())
	}()
	return written, errors.Join(err, client.close())
}
