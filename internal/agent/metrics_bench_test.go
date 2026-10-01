package agent

import (
	"os"
	"testing"
	"time"
)

// BenchmarkSampleContainer reads one container's counters, using this
// process's own cgroup and network namespace in place of a container's.
func BenchmarkSampleContainer(b *testing.B) {
	dir, err := cgroupDir(os.Getpid())
	if err != nil {
		b.Skip(err)
	}
	source := &usageSource{cgroupDir: dir, pid: os.Getpid()}
	now := time.Now()
	source.sample("c", now, nil)
	b.ResetTimer()
	for i := range b.N {
		if source.sample("c", now.Add(time.Duration(i+1)*time.Second), nil) == nil {
			b.Fatal("no sample")
		}
	}
}

func TestParseGPUs(t *testing.T) {
	gpus := parseGPUs([]byte("GPU-1, NVIDIA L4, 37, 1024, 23034\nGPU-2, NVIDIA L4, [N/A], 0, 23034\n"))
	g := gpus["GPU-1"]
	if len(gpus) != 2 || g.GetUtilizationPercent() != 37 || g.GetMemoryUsedBytes() != 1024<<20 || g.GetName() != "NVIDIA L4" {
		t.Fatalf("gpus %v", gpus)
	}
}
