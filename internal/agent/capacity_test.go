package agent

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

func TestGPUModelsUseThePlatformVocabulary(t *testing.T) {
	for name, want := range map[string]string{
		"Tesla T4":                  "T4",
		"NVIDIA A10G":               "A10G",
		"NVIDIA L4":                 "L4",
		"NVIDIA L40S":               "L40S",
		"NVIDIA L40":                "L40",
		"NVIDIA A100-SXM4-40GB":     "A100-40",
		"NVIDIA A100 80GB PCIe":     "A100-80",
		"NVIDIA H100 80GB HBM3":     "H100",
		"NVIDIA H200":               "H200",
		"NVIDIA GH200 480GB":        "GH200",
		"NVIDIA GeForce RTX 3090":   "RTX3090",
		"Quadro RTX 6000":           "RTX6000",
		"NVIDIA RTX A6000":          "RTXA6000",
		"  NVIDIA GeForce RTX 4090": "RTX4090",
	} {
		if got := gpuModel(name); got != want {
			t.Errorf("gpuModel(%q) = %q, want %q", name, got, want)
		}
	}
}

func TestOfferAppliesLimits(t *testing.T) {
	a10g := []gpuDevice{{"0", "GPU-a", "A10G"}, {"1", "GPU-b", "A10G"}}
	machine := detected{cpuMillis: 8000, memoryBytes: 16 << 30, gpus: a10g}
	cases := []struct {
		name    string
		machine detected
		limits  Limits
		check   string
		message string
		ok      bool
		gpus    int32
	}{
		{"detected cpu keeps a reserve", machine, Limits{}, "capacity.max_cpu", "using 7200m CPU", true, 2},
		{"cpu limit", machine, Limits{CPUMillis: 1500}, "capacity.max_cpu", "using 1500m CPU", true, 2},
		{"cpu above the machine", machine, Limits{CPUMillis: 9000}, "capacity.max_cpu", "requested CPU exceeds detected CPU", false, 2},
		{"memory limit", machine, Limits{MemoryBytes: 4 << 30}, "capacity.max_memory", "using 4096 MB memory", true, 2},
		{"memory above the machine", machine, Limits{MemoryBytes: 32 << 30}, "capacity.max_memory", "requested memory exceeds detected memory", false, 2},
		{"first gpus", machine, Limits{GPUs: new(1)}, "capacity.max_gpus", "using 1 GPUs", true, 1},
		{"no gpus", machine, Limits{GPUs: new(0)}, "capacity.max_gpus", "using 0 GPUs", true, 0},
		{"too many gpus", machine, Limits{GPUs: new(3)}, "capacity.max_gpus", "requested 3 GPUs, detected 2", false, 0},
		{"gpu by uuid and index", machine, Limits{GPUIDs: []string{"GPU-b", "1"}}, "capacity.gpu_ids", "using 1 GPUs", true, 1},
		{"unknown gpu id", machine, Limits{GPUIDs: []string{"7"}}, "capacity.gpu_ids", "GPU id '7' was not detected", false, 0},
		{"mixed models", detected{cpuMillis: 8000, memoryBytes: 16 << 30, gpus: append(a10g, gpuDevice{"2", "GPU-c", "T4"})}, Limits{}, "gpu.model", "the offered GPUs are of mixed models: A10G, T4", false, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			o := resolveOffer(c.machine, c.limits)
			var found *hostproto.PreflightCheck
			for _, check := range o.checks {
				if check.GetName() == c.check {
					found = check
				}
			}
			if found == nil || found.GetMessage() != c.message || found.GetOk() != c.ok {
				t.Fatalf("check %s = %v, want ok=%v %q", c.check, found, c.ok, c.message)
			}
			if !c.ok && (found.GetSeverity() != "error" || found.GetRemediation() == "") {
				t.Fatalf("a failed limit is an error with a remedy: %v", found)
			}
			if !nvidiaRuntimeInstalled() {
				return
			}
			if got := o.capacity.GetGpuCount(); got != c.gpus || (got > 0) != (o.capacity.GetGpuType() != "") {
				t.Fatalf("offered %d %q GPUs, want %d", got, o.capacity.GetGpuType(), c.gpus)
			}
		})
	}
}

func TestParseCapacityLimits(t *testing.T) {
	for value, want := range map[string]int64{"2": 2000, "1.5": 1500, "0.25": 250} {
		if got, err := ParseCPU(value); err != nil || got != want {
			t.Errorf("ParseCPU(%q) = %d, %v; want %d", value, got, err, want)
		}
	}
	for _, value := range []string{"0", "-1", "two", ""} {
		if _, err := ParseCPU(value); err == nil || err.Error() != "max cpu must be a positive number of cores" {
			t.Errorf("ParseCPU(%q) error %v", value, err)
		}
	}
	for value, want := range map[string]int64{"2048": 2048 << 20, "512mb": 512 << 20, "16gib": 16 << 30, "16Gi": 16 << 30, "2g": 2000 << 20, "1tib": 1 << 40, "1.5gb": 1500 << 20} {
		if got, err := ParseMemory(value); err != nil || got != want {
			t.Errorf("ParseMemory(%q) = %d, %v; want %d", value, got, err, want)
		}
	}
	if _, err := ParseMemory("lots"); err == nil {
		t.Error("ParseMemory accepted lots")
	}
}

func TestAgentEnrollsWithPreflightChecks(t *testing.T) {
	e := newEnv(t)
	e.startAgent()
	enroll := e.enrollment()
	s := e.session()
	if enroll.GetArchitecture() != runtime.GOARCH {
		t.Fatalf("architecture %q", enroll.GetArchitecture())
	}
	checks := map[string]*hostproto.PreflightCheck{}
	for _, check := range enroll.GetPreflight() {
		checks[check.GetName()] = check
	}
	for name, message := range map[string]string{"docker": "Docker is reachable", "capacity.max_cpu": "using 4000m CPU", "capacity.max_memory": "using 8192 MB memory"} {
		if check := checks[name]; !check.GetOk() || check.GetMessage() != message {
			t.Errorf("check %s = %v, want %q", name, check, message)
		}
	}
	gpus := detectGPUs(t.Context())
	if len(gpus) > 0 && nvidiaRuntimeInstalled() {
		capacity := enroll.GetCapacity()
		if capacity.GetGpuCount() != int32(len(gpus)) || capacity.GetGpuType() != gpus[0].Model {
			t.Fatalf("enrolled with %d %q GPUs, detected %v", capacity.GetGpuCount(), capacity.GetGpuType(), gpus)
		}
		t.Logf("this host offers %d %s", capacity.GetGpuCount(), capacity.GetGpuType())
	}
	if hello := s.hello.GetCapacity(); hello.GetGpuCount() != enroll.GetCapacity().GetGpuCount() || hello.GetCpuMillis() != 4000 {
		t.Fatalf("hello capacity %v, enrolled %v", hello, enroll.GetCapacity())
	}
}

func TestAgentWithAFailedPreflightEnrollsButDoesNotJoin(t *testing.T) {
	e := newEnv(t)
	tokenFile := filepath.Join(e.stateDir, "join-token")
	if err := os.WriteFile(tokenFile, []byte(e.server.joinToken+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	a := e.startAgent(func(cfg *Config) {
		cfg.JoinToken, cfg.JoinTokenFile = "", tokenFile
		cfg.Limits.CPUMillis = 1 << 40
	})
	enroll := e.enrollment()
	if enroll.GetJoinToken() != e.server.joinToken {
		t.Fatalf("enrolled with token %q", enroll.GetJoinToken())
	}
	failed := false
	for _, check := range enroll.GetPreflight() {
		failed = failed || (check.GetName() == "capacity.max_cpu" && !check.GetOk() && check.GetMessage() == "requested CPU exceeds detected CPU")
	}
	if !failed {
		t.Fatalf("enrollment did not report the failed CPU check: %v", enroll.GetPreflight())
	}
	if err := a.exited(t); !errors.Is(err, ErrPreflightFailed) {
		t.Fatalf("Run returned %v", err)
	}
	for _, path := range []string{tokenFile, identityPath(e.stateDir)} {
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Errorf("%s remains after a failed join: %v", path, err)
		}
	}
}
