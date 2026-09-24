package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	exportID   = "disk"
	exportName = "disk"
	qmpTimeout = 60 * time.Second
)

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
	if _, err := client.readMessage(); err != nil {
		conn.Close()
		return nil, fmt.Errorf("read monitor greeting: %w", err)
	}
	if err := client.execute("qmp_capabilities", nil, nil); err != nil {
		conn.Close()
		return nil, err
	}
	return client, nil
}

func (c *qmpClient) close() { c.conn.Close() }

func (c *qmpClient) readMessage() (map[string]json.RawMessage, error) {
	c.conn.SetReadDeadline(time.Now().Add(qmpTimeout))
	if !c.scanner.Scan() {
		if err := c.scanner.Err(); err != nil {
			return nil, err
		}
		return nil, errors.New("monitor closed the connection")
	}
	var message map[string]json.RawMessage
	if err := json.Unmarshal(c.scanner.Bytes(), &message); err != nil {
		return nil, fmt.Errorf("parse monitor message: %w", err)
	}
	return message, nil
}

func (c *qmpClient) execute(name string, args any, out any) error {
	request := map[string]any{"execute": name}
	if args != nil {
		request["arguments"] = args
	}
	payload, err := json.Marshal(request)
	if err != nil {
		return err
	}
	c.conn.SetWriteDeadline(time.Now().Add(qmpTimeout))
	if _, err := c.conn.Write(append(payload, '\n')); err != nil {
		return fmt.Errorf("monitor %s: %w", name, err)
	}
	for {
		message, err := c.readMessage()
		if err != nil {
			return fmt.Errorf("monitor %s: %w", name, err)
		}
		if _, isEvent := message["event"]; isEvent {
			continue
		}
		if raw, failed := message["error"]; failed {
			var qerr qmpError
			json.Unmarshal(raw, &qerr)
			return fmt.Errorf("monitor %s: %s: %s", name, qerr.Class, qerr.Desc)
		}
		raw, ok := message["return"]
		if !ok {
			return fmt.Errorf("monitor %s: unexpected reply %v", name, message)
		}
		if out != nil {
			if err := json.Unmarshal(raw, out); err != nil {
				return fmt.Errorf("monitor %s: parse reply: %w", name, err)
			}
		}
		return nil
	}
}

func fileChild(path string) map[string]any {
	return map[string]any{"driver": "file", "filename": path}
}

// chainNode describes layers[0..top] as one nested blockdev with every node
// named, so seal and compact can address layers directly.
func chainNode(p diskPaths, layers []layer, top int) map[string]any {
	node := map[string]any{
		"driver":    layers[top].format(),
		"node-name": layers[top].node(),
		"file":      fileChild(p.layerPath(layers[top])),
	}
	if layers[top].Lazy {
		// Read through `serve`, which fetches what the file does not hold yet.
		node["read-only"] = true
		node["file"] = map[string]any{
			"driver":    "nbd",
			"read-only": true,
			"server":    map[string]any{"type": "unix", "path": p.layersSocket()},
			"export":    layers[top].file(),
		}
	}
	if top == 0 {
		// A raw base takes no backing option at all.
		if !layers[0].Raw {
			node["backing"] = nil
		}
	} else {
		node["backing"] = chainNode(p, layers, top-1)
	}
	return node
}

func startDaemon(ctx context.Context, p diskPaths, state *diskState) (int, error) {
	if err := p.checkSocketPaths(); err != nil {
		return 0, err
	}
	if err := os.MkdirAll(p.runDir(), 0o700); err != nil {
		return 0, err
	}
	for _, stale := range []string{p.qmpSocket(), p.nbdSocket(), p.pidFile()} {
		if err := os.Remove(stale); err != nil && !errors.Is(err, os.ErrNotExist) {
			return 0, err
		}
	}
	head := chainNode(p, state.Layers, len(state.Layers)-1)
	head["discard"] = "unmap"
	// Compaction commits zeroes a discard left in the head into the layer it
	// compacts into, the lowest one the daemon opens as a file; detecting them
	// there frees its space instead of writing them.
	target := state.lowestLocal()
	base := head
	for i := len(state.Layers) - 1; i > target; i-- {
		base = base["backing"].(map[string]any)
	}
	if target < len(state.Layers)-1 {
		base["discard"] = "unmap"
		base["detect-zeroes"] = "unmap"
	}
	graph, err := json.Marshal(head)
	if err != nil {
		return 0, err
	}
	_, err = runTool(ctx, toolDaemon,
		"--daemonize",
		"--pidfile", p.pidFile(),
		"--chardev", "socket,id=monitor,path="+p.qmpSocket()+",server=on,wait=off",
		"--monitor", "chardev=monitor",
		"--blockdev", string(graph),
		"--nbd-server", "addr.type=unix,addr.path="+p.nbdSocket(),
		"--export", "type=nbd,id="+exportID+",node-name="+state.head().node()+",name="+exportName+",writable=on",
	)
	if err != nil {
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
	if pid <= 0 {
		return false
	}
	cmdline, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "cmdline"))
	if err != nil {
		return false
	}
	return strings.Contains(string(cmdline), p.qmpSocket())
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
		defer client.close()
		return client.execute("quit", nil, nil)
	}()
	if quitErr != nil {
		if err := syscall.Kill(pid, syscall.SIGTERM); err != nil && !errors.Is(err, syscall.ESRCH) {
			return fmt.Errorf("stop qemu-storage-daemon %d: quit failed (%v) and SIGTERM failed: %w", pid, quitErr, err)
		}
	}
	deadline := time.Now().Add(30 * time.Second)
	for daemonAlive(p, pid) {
		if time.Now().After(deadline) {
			return fmt.Errorf("qemu-storage-daemon %d did not exit within 30s", pid)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(50 * time.Millisecond):
		}
	}
	return nil
}

type blockStats struct {
	NodeName string `json:"node-name"`
	Stats    struct {
		WrHighestOffset int64 `json:"wr_highest_offset"`
	} `json:"stats"`
}

// filesystemBlockBytes is the ext4 block size formatExt4 pins. Block 0 holds
// the superblock and nothing else a workload writes.
const filesystemBlockBytes = 4096

// headWritten reports whether the head took a write past the superblock since
// the daemon opened it. Every thaw rewrites the superblock, so each new head
// receives that one block within milliseconds of a seal; counting it would
// seal a layer every cycle on an idle disk. Any change a workload makes
// writes inodes, bitmaps or data beyond block 0. The export's own backend is
// anonymous and absent from the statistics, so this reads the node's
// high-water mark.
func headWritten(client *qmpClient, node string) (bool, error) {
	var stats []blockStats
	if err := client.execute("query-blockstats", map[string]any{"query-nodes": true}, &stats); err != nil {
		return false, err
	}
	for _, entry := range stats {
		if entry.NodeName == node {
			return entry.Stats.WrHighestOffset > filesystemBlockBytes, nil
		}
	}
	return false, fmt.Errorf("qemu-storage-daemon has no node %s", node)
}

func exportedNode(client *qmpClient) (string, error) {
	var exports []struct {
		ID       string `json:"id"`
		NodeName string `json:"node-name"`
	}
	if err := client.execute("query-block-exports", nil, &exports); err != nil {
		return "", err
	}
	for _, export := range exports {
		if export.ID == exportID {
			return export.NodeName, nil
		}
	}
	return "", errors.New("qemu-storage-daemon has no disk export")
}
