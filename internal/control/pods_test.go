package control

import (
	"errors"
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

func pod(name string, kind apitypes.PodKind) apitypes.WorkloadSpec {
	spec := function(name)
	spec.Kind, spec.Handler = apitypes.WorkloadKindPod, nil
	if kind == apitypes.PodKindSandbox {
		spec.Kind = apitypes.WorkloadKindSandbox
	}
	spec.Pod = &apitypes.PodSpec{Kind: kind, Command: &[]string{"python", "-m", "http.server", "8080"}, Ports: &map[string]int{"http": 8080}}
	return spec
}

func TestPodDefinitionsResolveTheirDefaults(t *testing.T) {
	cases := []struct {
		name       string
		spec       func() apitypes.WorkloadSpec
		keepWarm   int
		authorized bool
		ssh        bool
	}{
		{"pod", func() apitypes.WorkloadSpec { return pod("web", apitypes.PodKindPod) }, 600, false, false},
		{"sandbox", func() apitypes.WorkloadSpec { return pod("box", apitypes.PodKindSandbox) }, 600, false, false},
		{"devbox", func() apitypes.WorkloadSpec {
			s := pod("dev", apitypes.PodKindDevbox)
			s.Disks = &[]apitypes.DiskMountSpec{{Name: "dev", SizeBytes: 1 << 30, MountPath: "/"}}
			return s
		}, 1800, false, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			spec := c.spec()
			out, err := Resolve(spec)
			if err != nil {
				t.Fatalf("resolve: %v", err)
			}
			if *out.KeepWarmSeconds != c.keepWarm || *out.Authorized != c.authorized || *out.Pod.Ssh != c.ssh {
				t.Fatalf("resolved keep_warm %d authorized %v ssh %v", *out.KeepWarmSeconds, *out.Authorized, *out.Pod.Ssh)
			}
			if c.name == "devbox" && (out.Placement == nil || *out.Placement.Preemptible) {
				t.Fatal("a devbox may be preempted")
			}
		})
	}
}

func TestPodDefinitionsRejectWhatTheyCannotRun(t *testing.T) {
	withDisk := func(s apitypes.WorkloadSpec, name, path string) apitypes.WorkloadSpec {
		s.Disks = &[]apitypes.DiskMountSpec{{Name: name, SizeBytes: 1 << 30, MountPath: path}}
		return s
	}
	cases := map[string]struct {
		spec   apitypes.WorkloadSpec
		reason string
	}{
		"handler": {func() apitypes.WorkloadSpec { s := pod("web", apitypes.PodKindPod); s.Handler = new("a:b"); return s }(), "not a handler"},
		"kind of another section": {func() apitypes.WorkloadSpec {
			s := pod("box", apitypes.PodKindSandbox)
			s.Kind = apitypes.WorkloadKindPod
			return s
		}(), "does not match"},
		"function without one": {func() apitypes.WorkloadSpec { s := function("f"); s.Handler = nil; return s }(), "handler is required"},
		"devbox without disk":  {pod("dev", apitypes.PodKindDevbox), "needs a root disk"},
		"devbox ssh off": {func() apitypes.WorkloadSpec {
			s := withDisk(pod("dev", apitypes.PodKindDevbox), "dev", "/")
			s.Pod.Ssh = new(false)
			return s
		}(), "cannot turn ssh off"},
		"root disk on a pod": {withDisk(pod("web", apitypes.PodKindPod), "web", "/"), "only a devbox"},
		"private tcp": {func() apitypes.WorkloadSpec {
			s := pod("web", apitypes.PodKindPod)
			s.Pod.Tcp, s.Authorized = new(true), new(true)
			return s
		}(), "requires a public Pod"},
		"docker with a network policy": {func() apitypes.WorkloadSpec {
			s := pod("web", apitypes.PodKindSandbox)
			s.DockerEnabled, s.Pod.BlockNetwork = new(true), new(true)
			return s
		}(), "cannot be combined"},
		"bad cidr": {func() apitypes.WorkloadSpec {
			s := pod("web", apitypes.PodKindPod)
			s.Pod.AllowList = &[]string{"example.com"}
			return s
		}(), "not an IP address"},
		"checkpoint without readiness": {func() apitypes.WorkloadSpec {
			s := pod("web", apitypes.PodKindPod)
			s.Checkpoint = &apitypes.CheckpointSpec{}
			return s
		}(), "checkpoint_readiness_path"},
		"disk with two containers": {func() apitypes.WorkloadSpec {
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

func TestWorkloadsOfEveryKindReadTheirActiveRelease(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	deploy(t, c, ws, false, pod("web", apitypes.PodKindPod), pod("scratch", apitypes.PodKindSandbox))
	page, err := c.ListWorkloads(t.Context(), ws, WorkloadFilter{}, 10, "")
	if err != nil || len(page.Workloads) != 2 {
		t.Fatalf("deployments %+v %v", page.Workloads, err)
	}
	for _, d := range page.Workloads {
		release, err := c.Release(t.Context(), ws, WorkloadID(d.Id), nil)
		if err != nil {
			t.Fatalf("%s release: %v", d.Name, err)
		}
		if release.Id != *d.ReleaseId || release.Spec.Pod == nil || (*release.Spec.Pod.Ports)["http"] != 8080 {
			t.Fatalf("%s (%s) release %+v, want %v with its pod section", d.Name, d.Kind, release, *d.ReleaseId)
		}
	}
	if _, err := c.Release(t.Context(), ws, WorkloadID(uuid.New()), nil); !errors.Is(err, ErrVersionNotFound) {
		t.Fatalf("release of an unknown workload: %v", err)
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
	release, err := c.PrepareRelease(t.Context(), ws, "reports", pod("box", apitypes.PodKindSandbox))
	if err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(t.Context(), "select w.kind from workloads w join releases r on r.workload_id = w.id where r.id = $1", release.Id).Scan(&kind); err != nil || kind != "sandbox" {
		t.Fatalf("prepared kind %q, %v", kind, err)
	}
}
