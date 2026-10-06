package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"

	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

const (
	snapshotterExecutable = "lazycloud-snapshotter"
	snapshotterUnitName   = "lazycloud-snapshotter.service"
	// snapshotterBinary is a copy outside the agent's releases, which
	// updates replace and prune while the snapshotter keeps running.
	snapshotterBinary = "/usr/local/lib/lazycloud/" + snapshotterExecutable
	snapshotterRoot   = "/var/lib/lazycloud-snapshotter"
	containerdConfig  = "/etc/containerd/config.toml"
	dockerConfig      = "/etc/docker/daemon.json"
	// minDockerMajor is the first Docker with the containerd image store
	// and its snapshotter as storage driver.
	minDockerMajor = 25
)

// snapshotterUnit runs the snapshotter before containerd and Docker. Its
// FUSE mounts die with its process, so stopping or restarting the unit
// kills that process only, and nothing but the node image or an operator
// restarts it.
const snapshotterUnit = `[Unit]
Description=LazyCloud snapshotter: lazily read image layers for containerd
Before=containerd.service docker.service

[Service]
Type=notify
ExecStart=` + snapshotterBinary + ` --root=` + snapshotterRoot + `
Restart=always
RestartSec=2
KillMode=process
LimitNOFILE=1048576
OOMScoreAdjust=-999

[Install]
WantedBy=multi-user.target containerd.service
`

// containerdProxy registers the snapshotter with containerd.
const containerdProxy = `
[proxy_plugins.` + layersource.Snapshotter + `]
  type = "snapshot"
  address = "` + layersource.Socket + `"
  [proxy_plugins.` + layersource.Snapshotter + `.exports]
    root = "` + snapshotterRoot + `"
`

// installSnapshotter installs the snapshotter from release and points
// containerd and Docker at it: the containerd image store, the snapshotter
// as storage driver and live-restore. A running snapshotter is left alone,
// since restarting it breaks every container on the host. containerd and
// Docker restart only when their settings change, which stops containers
// that run then; Docker images pulled under another storage driver stay
// hidden while this one is set.
func installSnapshotter(release string, out io.Writer) error {
	if err := checkDocker(); err != nil {
		return err
	}
	if _, err := os.Stat("/run/containerd/containerd.sock"); err != nil {
		return fmt.Errorf("the snapshotter needs Docker on the system containerd at /run/containerd/containerd.sock: %w", err)
	}
	active := exec.CommandContext(context.Background(), "systemctl", "is-active", "--quiet", snapshotterUnitName).Run() == nil //nolint:gosec // a fixed unit name
	if active {
		_, _ = fmt.Fprintln(out, "lazycloud-snapshotter is running and keeps its version; it changes on a drained host")
	} else if err := copyFile(filepath.Join(release, snapshotterExecutable), snapshotterBinary); err != nil {
		return err
	}
	if _, err := writeIfChanged(filepath.Join(systemdUnits, snapshotterUnitName), []byte(snapshotterUnit), 0o644); err != nil {
		return err
	}
	containerdChanged, err := addContainerdProxy(containerdConfig)
	if err != nil {
		return err
	}
	dockerChanged, err := setDockerStorage(dockerConfig)
	if err != nil {
		return err
	}
	commands := [][]string{{"daemon-reload"}, {"enable", snapshotterUnitName}}
	if !active {
		commands = append(commands, []string{"start", snapshotterUnitName})
	}
	if containerdChanged {
		commands = append(commands, []string{"restart", "containerd.service"})
	}
	if containerdChanged || dockerChanged {
		_, _ = fmt.Fprintln(out, "restarting Docker on the lazycloud storage driver")
		commands = append(commands, []string{"restart", "docker.service"})
	}
	for _, command := range commands {
		cmd := exec.CommandContext(context.Background(), "systemctl", command...) //nolint:gosec // fixed subcommands and unit names
		cmd.Stdout, cmd.Stderr = out, out
		if err := cmd.Run(); err != nil {
			return fmt.Errorf("systemctl %s: %w", strings.Join(command, " "), err)
		}
	}
	return nil
}

func checkDocker() error {
	version, err := exec.CommandContext(context.Background(), "docker", "version", "--format", "{{.Server.Version}}").Output()
	if err != nil {
		return fmt.Errorf("read the Docker version: %w", err)
	}
	major, _, _ := strings.Cut(strings.TrimSpace(string(version)), ".")
	if n, err := strconv.Atoi(major); err != nil || n < minDockerMajor {
		return fmt.Errorf("docker %s is too old; the snapshotter needs Docker %d or later", strings.TrimSpace(string(version)), minDockerMajor)
	}
	return nil
}

// addContainerdProxy appends the snapshotter's proxy plugin to containerd's
// configuration unless it is there, and reports whether it changed.
func addContainerdProxy(path string) (bool, error) {
	current, err := os.ReadFile(path) //nolint:gosec // containerd's fixed configuration path
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return false, fmt.Errorf("read containerd configuration: %w", err)
	}
	if bytes.Contains(current, []byte("[proxy_plugins."+layersource.Snapshotter+"]")) {
		return false, nil
	}
	return writeIfChanged(path, append(current, containerdProxy...), 0o644)
}

// setDockerStorage sets the containerd image store, the snapshotter as
// storage driver and live-restore in Docker's daemon.json, keeping its
// other settings, and reports whether it changed.
func setDockerStorage(path string) (bool, error) {
	current, err := os.ReadFile(path) //nolint:gosec // Docker's fixed configuration path
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return false, fmt.Errorf("read Docker configuration: %w", err)
	}
	original, config := map[string]any{}, map[string]any{}
	if len(bytes.TrimSpace(current)) > 0 {
		for _, into := range []*map[string]any{&original, &config} {
			if err := json.Unmarshal(current, into); err != nil {
				return false, fmt.Errorf("parse %s: %w", path, err)
			}
		}
	}
	features, _ := config["features"].(map[string]any)
	if features == nil {
		features = map[string]any{}
	}
	features["containerd-snapshotter"] = true
	config["features"] = features
	config["storage-driver"] = layersource.Snapshotter
	config["live-restore"] = true
	if reflect.DeepEqual(original, config) {
		return false, nil
	}
	next, err := json.MarshalIndent(config, "", "    ")
	if err != nil {
		return false, fmt.Errorf("encode Docker configuration: %w", err)
	}
	return writeIfChanged(path, append(next, '\n'), 0o644)
}

// writeIfChanged replaces path with data unless it holds it already.
func writeIfChanged(path string, data []byte, perm os.FileMode) (bool, error) {
	if current, err := os.ReadFile(path); err == nil && bytes.Equal(current, data) { //nolint:gosec // fixed configuration paths
		return false, nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil { //nolint:gosec // configuration directories are world-readable
		return false, fmt.Errorf("create %s: %w", filepath.Dir(path), err)
	}
	tmp := path + ".lazycloud.tmp"
	if err := os.WriteFile(tmp, data, perm); err != nil { //nolint:gosec // fixed configuration paths
		return false, fmt.Errorf("write %s: %w", path, err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return false, fmt.Errorf("write %s: %w", path, err)
	}
	return true, nil
}

// installSnapshotterCommand installs the snapshotter found beside the
// running executable.
func installSnapshotterCommand() error {
	if os.Geteuid() != 0 {
		return errors.New("install-snapshotter requires root")
	}
	exe, err := os.Executable()
	if err != nil {
		return fmt.Errorf("find the agent executable: %w", err)
	}
	if exe, err = filepath.EvalSymlinks(exe); err != nil {
		return fmt.Errorf("resolve the agent executable: %w", err)
	}
	return installSnapshotter(filepath.Dir(exe), os.Stderr)
}

func copyFile(from, to string) error {
	data, err := os.ReadFile(from) //nolint:gosec // the snapshotter inside the agent's own release
	if err != nil {
		return fmt.Errorf("read the snapshotter from the agent release: %w", err)
	}
	if _, err := writeIfChanged(to, data, 0o755); err != nil {
		return err
	}
	return nil
}
