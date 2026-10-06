package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/pelletier/go-toml/v2"

	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

// The snapshotter's settings join the host's containerd and Docker
// configuration without dropping what is there, once.
func TestSnapshotterSettingsKeepTheHostsConfiguration(t *testing.T) {
	dir := t.TempDir()
	containerd := filepath.Join(dir, "config.toml")
	if err := os.WriteFile(containerd, []byte("version = 3\n\n[proxy_plugins]\n  [proxy_plugins.soci]\n    type = \"snapshot\"\n    address = \"/run/soci.sock\"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	docker := filepath.Join(dir, "daemon.json")
	if err := os.WriteFile(docker, []byte(`{"runtimes": {"runsc": {"path": "/usr/local/bin/runsc"}}, "features": {"cdi": true}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	for run, wantChange := range []bool{true, false} {
		if changed, err := addContainerdProxy(containerd); err != nil || changed != wantChange {
			t.Fatalf("run %d: containerd changed %v, %v", run, changed, err)
		}
		if changed, err := setDockerStorage(docker); err != nil || changed != wantChange {
			t.Fatalf("run %d: Docker changed %v, %v", run, changed, err)
		}
	}

	raw, err := os.ReadFile(containerd)
	if err != nil {
		t.Fatal(err)
	}
	var c struct {
		Version      int
		ProxyPlugins map[string]struct {
			Type, Address string
		} `toml:"proxy_plugins"`
	}
	if err := toml.Unmarshal(raw, &c); err != nil {
		t.Fatalf("containerd configuration: %v\n%s", err, raw)
	}
	if c.Version != 3 || c.ProxyPlugins["soci"].Address != "/run/soci.sock" ||
		c.ProxyPlugins[layersource.Snapshotter].Address != layersource.Socket || c.ProxyPlugins[layersource.Snapshotter].Type != "snapshot" {
		t.Fatalf("containerd configuration is %+v", c)
	}

	raw, err = os.ReadFile(docker)
	if err != nil {
		t.Fatal(err)
	}
	var d struct {
		Runtimes      map[string]struct{ Path string }
		Features      map[string]bool
		StorageDriver string `json:"storage-driver"`
		LiveRestore   bool   `json:"live-restore"`
	}
	if err := json.Unmarshal(raw, &d); err != nil {
		t.Fatal(err)
	}
	if d.Runtimes["runsc"].Path != "/usr/local/bin/runsc" || !d.Features["cdi"] || !d.Features["containerd-snapshotter"] ||
		d.StorageDriver != layersource.Snapshotter || !d.LiveRestore {
		t.Fatalf("Docker configuration is %s", raw)
	}
}
