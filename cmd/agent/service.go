package main

import (
	"context"
	_ "embed"
	"errors"
	"flag"
	"fmt"
	"maps"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"github.com/AmbientWare/lazycloud/internal/agent"
)

// serviceWrapper starts the current release and rolls back an update that
// does not connect.
//
//go:embed agent-service.sh
var serviceWrapper string

const (
	wrapperName   = "agent-service.sh"
	joinTokenName = "join-token"
	systemdUnits  = "/etc/systemd/system"
)

var serviceName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.@-]*$`)

// installService writes the join token, the wrapper and a systemd unit that
// runs `join` with the given flags, then enables and restarts the unit.
func installService(args []string) error {
	j := newJoinFlags("install-service")
	name := j.set.String("service-name", "lazycloud-agent", "systemd unit name")
	if err := j.parse(args); err != nil {
		return err
	}
	if !serviceName.MatchString(*name) {
		return fmt.Errorf("service name %q is not a unit name", *name)
	}
	if os.Geteuid() != 0 {
		return errors.New("install-service requires root")
	}
	if info, err := os.Stat("/run/systemd/system"); err != nil || !info.IsDir() {
		return errors.New("install-service requires systemd")
	}
	root, err := releaseRoot(j.cfg.Executable)
	if err != nil {
		return err
	}
	state := j.cfg.StateDir
	if err := os.MkdirAll(state, 0o700); err != nil {
		return fmt.Errorf("create state directory: %w", err)
	}
	if err := os.Chmod(state, 0o700); err != nil { //nolint:gosec // a directory the agent traverses
		return fmt.Errorf("restrict state directory: %w", err)
	}
	joinArgs := serviceArgs(j)
	if j.cfg.JoinToken != "" {
		tokenFile := filepath.Join(state, joinTokenName)
		if err := os.WriteFile(tokenFile, []byte(j.cfg.JoinToken+"\n"), 0o600); err != nil {
			return fmt.Errorf("write join token: %w", err)
		}
		joinArgs = append(joinArgs, "--join-token-file="+tokenFile)
	}
	wrapper := filepath.Join(root, wrapperName)
	if err := os.WriteFile(wrapper, []byte(serviceWrapper), 0o755); err != nil { //nolint:gosec // the wrapper is executable
		return fmt.Errorf("write service wrapper: %w", err)
	}
	// The agent's preflight refuses a host without the snapshotter.
	if err := installSnapshotter(j.cfg.Executable, os.Stderr); err != nil {
		return err
	}
	unit := filepath.Join(systemdUnits, *name+".service")
	if err := os.WriteFile(unit, []byte(renderUnit(root, state, wrapper, joinArgs)), 0o644); err != nil { //nolint:gosec // units are world-readable
		return fmt.Errorf("write systemd unit: %w", err)
	}
	for _, command := range [][]string{{"daemon-reload"}, {"enable", *name + ".service"}, {"restart", *name + ".service"}} {
		cmd := exec.CommandContext(context.Background(), "systemctl", command...) //nolint:gosec // fixed subcommands and a checked unit name
		cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
		if err := cmd.Run(); err != nil {
			return fmt.Errorf("systemctl %s: %w", strings.Join(command, " "), err)
		}
	}
	return nil
}

// releaseRoot is the install root of an executable at
// <root>/releases/<version>/lazycloud-agent.
func releaseRoot(executable string) (string, error) {
	exe, err := resolveAgent(executable)
	if err != nil {
		return "", err
	}
	releases := filepath.Dir(filepath.Dir(exe))
	if filepath.Base(releases) != "releases" || filepath.Base(exe) != agent.AgentExecutable {
		return "", fmt.Errorf("install-service runs from an installed release, <root>/releases/<version>/%s; this is %s", agent.AgentExecutable, exe)
	}
	return filepath.Dir(releases), nil
}

// resolveAgent follows the links to the agent executable, whose release
// directory holds the binaries it installs.
func resolveAgent(executable string) (string, error) {
	exe, err := filepath.EvalSymlinks(executable)
	if err != nil {
		return "", fmt.Errorf("resolve the agent executable: %w", err)
	}
	return exe, nil
}

// serviceArgs restates the flags given on the command line for the unit,
// with paths made absolute. The state directory travels in the environment,
// which the wrapper also reads, and the join token in its file.
func serviceArgs(j *joinFlags) []string {
	cfg := j.cfg
	resolved := map[string]string{"runtime-dir": cfg.RuntimeDir, "supervisor": cfg.SupervisorPath, "join-token-file": cfg.JoinTokenFile}
	args := []string{"join", "--server=" + cfg.Server}
	j.set.Visit(func(f *flag.Flag) {
		switch f.Name {
		case "server", "join-token", "service-name", "state-dir", "label":
			return
		}
		value, ok := resolved[f.Name]
		if !ok {
			value = f.Value.String()
		}
		args = append(args, "--"+f.Name+"="+value)
	})
	for _, key := range slices.Sorted(maps.Keys(cfg.Labels)) {
		args = append(args, "--label="+key+"="+cfg.Labels[key])
	}
	return args
}

func renderUnit(root, state, wrapper string, args []string) string {
	command := append([]string{"/bin/sh", wrapper}, args...)
	quoted := make([]string, len(command))
	for i, arg := range command {
		// ExecStart expands $NAME; Environment= does not.
		quoted[i] = strings.ReplaceAll(systemdQuote(arg), "$", "$$")
	}
	return strings.Join([]string{
		"[Unit]",
		"Description=LazyCloud agent",
		"Wants=docker.service network-online.target " + snapshotterUnitName,
		"After=docker.service network-online.target " + snapshotterUnitName,
		"",
		"[Service]",
		"Type=simple",
		"Environment=" + systemdQuote("LAZYCLOUD_AGENT_ROOT="+root),
		"Environment=" + systemdQuote("LAZYCLOUD_AGENT_STATE_DIR="+state),
		"ExecStart=" + strings.Join(quoted, " "),
		"Restart=on-failure",
		// 78 means restarting cannot help, as after the machine was removed.
		"RestartPreventExitStatus=78",
		"RestartSec=15",
		"KillSignal=SIGINT",
		"TimeoutStopSec=30",
		"LimitNOFILE=1048576",
		// Containers get every core. On a joined machine Docker puts them
		// beside the agent in system.slice, each weighing about 40 per
		// reserved core; fleet hosts give them a slice of their own
		// (deploy/ami/node-setup.sh).
		"CPUWeight=1000",
		"",
		"[Install]",
		"WantedBy=multi-user.target",
		"",
	}, "\n")
}

func systemdQuote(value string) string {
	r := strings.NewReplacer(`\`, `\\`, `"`, `\"`, "\n", `\n`, "%", "%%")
	return `"` + r.Replace(value) + `"`
}
