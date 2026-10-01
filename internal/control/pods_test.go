package control

import (
	"errors"
	"strings"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

func pod(name string, kind apitypes.PodKind) apitypes.FunctionSpec {
	spec := function(name)
	spec.Handler = nil
	spec.Pod = &apitypes.PodSpec{Kind: kind, Command: &[]string{"python", "-m", "http.server", "8080"}, Ports: &map[string]int{"http": 8080}}
	return spec
}

func TestPodDefinitionsResolveTheirDefaults(t *testing.T) {
	cases := []struct {
		name       string
		spec       func() apitypes.FunctionSpec
		keepWarm   int
		authorized bool
		ssh        bool
		kind       apitypes.WorkloadKind
	}{
		{"pod", func() apitypes.FunctionSpec { return pod("web", apitypes.PodKindPod) }, 600, false, false, apitypes.WorkloadKindPod},
		{"sandbox", func() apitypes.FunctionSpec { return pod("box", apitypes.PodKindSandbox) }, 600, false, false, apitypes.WorkloadKindSandbox},
		{"devbox", func() apitypes.FunctionSpec {
			s := pod("dev", apitypes.PodKindDevbox)
			s.Disks = &[]apitypes.DiskMountSpec{{Name: "dev", SizeBytes: 1 << 30, MountPath: "/"}}
			return s
		}, 1800, false, true, apitypes.WorkloadKindPod},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			spec := c.spec()
			out, err := Resolve(spec)
			if err != nil {
				t.Fatalf("resolve: %v", err)
			}
			if *out.KeepWarmSeconds != c.keepWarm || *out.Authorized != c.authorized || *out.Pod.Ssh != c.ssh || KindOf(out) != c.kind {
				t.Fatalf("resolved keep_warm %d authorized %v ssh %v kind %s", *out.KeepWarmSeconds, *out.Authorized, *out.Pod.Ssh, KindOf(out))
			}
			if c.name == "devbox" && (out.Placement == nil || *out.Placement.Preemptible) {
				t.Fatal("a devbox may be preempted")
			}
		})
	}
}

func TestPodDefinitionsRejectWhatTheyCannotRun(t *testing.T) {
	withDisk := func(s apitypes.FunctionSpec, name, path string) apitypes.FunctionSpec {
		s.Disks = &[]apitypes.DiskMountSpec{{Name: name, SizeBytes: 1 << 30, MountPath: path}}
		return s
	}
	cases := map[string]struct {
		spec   apitypes.FunctionSpec
		reason string
	}{
		"handler":              {func() apitypes.FunctionSpec { s := pod("web", apitypes.PodKindPod); s.Handler = new("a:b"); return s }(), "not a handler"},
		"function without one": {func() apitypes.FunctionSpec { s := function("f"); s.Handler = nil; return s }(), "handler is required"},
		"devbox without disk":  {pod("dev", apitypes.PodKindDevbox), "needs a root disk"},
		"devbox ssh off": {func() apitypes.FunctionSpec {
			s := withDisk(pod("dev", apitypes.PodKindDevbox), "dev", "/")
			s.Pod.Ssh = new(false)
			return s
		}(), "cannot turn ssh off"},
		"root disk on a pod": {withDisk(pod("web", apitypes.PodKindPod), "web", "/"), "only a devbox"},
		"private tcp": {func() apitypes.FunctionSpec {
			s := pod("web", apitypes.PodKindPod)
			s.Pod.Tcp, s.Authorized = new(true), new(true)
			return s
		}(), "requires a public Pod"},
		"bad cidr": {func() apitypes.FunctionSpec {
			s := pod("web", apitypes.PodKindPod)
			s.Pod.AllowList = &[]string{"example.com"}
			return s
		}(), "not an IP address"},
		"checkpoint without readiness": {func() apitypes.FunctionSpec {
			s := pod("web", apitypes.PodKindPod)
			s.Checkpoint = &apitypes.CheckpointSpec{}
			return s
		}(), "checkpoint_readiness_path"},
		"disk with two containers": {func() apitypes.FunctionSpec {
			s := withDisk(pod("web", apitypes.PodKindPod), "data", "/data")
			s.Autoscaler = &apitypes.Autoscaler{MaxContainers: new(2)}
			return s
		}(), "runs one container"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := Resolve(c.spec)
			var invalid *InvalidSpecError
			if !errors.As(err, &invalid) || !strings.Contains(invalid.Reason, c.reason) {
				t.Fatalf("resolve = %v, want %q", err, c.reason)
			}
		})
	}
}

func TestPodsDeployAndPrepareUnderTheirKind(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	d := deploy(t, c, ws, false, pod("web", apitypes.PodKindPod))
	var kind string
	if err := pool.QueryRow(t.Context(), "select w.kind from workloads w join releases r on r.workload_id = w.id where r.id = $1", d.Releases[0].Id).Scan(&kind); err != nil || kind != "pod" {
		t.Fatalf("deployed kind %q, %v", kind, err)
	}
	release, err := c.PrepareRelease(t.Context(), ws, "reports", "box", pod("box", apitypes.PodKindSandbox))
	if err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(t.Context(), "select w.kind from workloads w join releases r on r.workload_id = w.id where r.id = $1", release.Id).Scan(&kind); err != nil || kind != "sandbox" {
		t.Fatalf("prepared kind %q, %v", kind, err)
	}
}
