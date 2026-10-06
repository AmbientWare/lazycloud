package agent

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/cpu"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	// defaultMetricsInterval paces sampling. Each pass reads a few cgroup
	// and proc files per running container and sends one message per host.
	defaultMetricsInterval = 5 * time.Second
	// cgroupRoot is where the unified cgroup v2 hierarchy is mounted.
	cgroupRoot = "/sys/fs/cgroup"
	// gpuSampleTimeout bounds one nvidia-smi call.
	gpuSampleTimeout = 2 * time.Second
)

// usageSource is where a running container's use is read, and the counters
// of the previous sample. Only the sampling goroutine reads or writes prev.
type usageSource struct {
	cgroupDir string
	pid       int
	// gpus are the UUIDs of the GPUs Docker gave the container.
	gpus []string
	prev *usageCounters
}

type usageCounters struct {
	at                 time.Time
	cpuUsec            uint64
	diskRead, diskWrit uint64
	netRx, netTx       uint64
}

// watchUsage finds the container's cgroup from its init process, so the
// sampler can read it without calling Docker. Under cgroup v1 it finds none
// and the container goes unsampled.
func (c *container) watchUsage(ctx context.Context) {
	inspect, err := c.a.docker.ContainerInspect(ctx, c.dockerName(), client.ContainerInspectOptions{})
	if err != nil || inspect.Container.State == nil || inspect.Container.State.Pid == 0 {
		c.log.Warn("container metrics are unavailable: cannot inspect the container", "error", err)
		return
	}
	pid := inspect.Container.State.Pid
	dir, err := cgroupDir(pid)
	if err != nil {
		c.log.Warn("container metrics are unavailable", "error", err)
		return
	}
	source := &usageSource{cgroupDir: dir, pid: pid}
	if hc := inspect.Container.HostConfig; hc != nil {
		for _, req := range hc.DeviceRequests {
			source.gpus = append(source.gpus, req.DeviceIDs...)
		}
	}
	c.usage.Store(source)
}

// cgroupDir is the cgroup v2 directory of pid.
func cgroupDir(pid int) (string, error) {
	data, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "cgroup"))
	if err != nil {
		return "", fmt.Errorf("read the cgroup of pid %d: %w", pid, err)
	}
	for line := range strings.SplitSeq(string(data), "\n") {
		if path, ok := strings.CutPrefix(line, "0::"); ok {
			return filepath.Join(cgroupRoot, filepath.Clean("/"+path)), nil
		}
	}
	return "", fmt.Errorf("pid %d has no cgroup v2 hierarchy", pid)
}

// sampleUsage sends one sample of every running container per interval
// until ctx ends.
func (a *Agent) sampleUsage(ctx context.Context) {
	interval := a.cfg.MetricsInterval
	if interval <= 0 {
		interval = defaultMetricsInterval
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		began := time.Now()
		a.mu.Lock()
		containers := make([]*container, 0, len(a.containers))
		for _, c := range a.containers {
			containers = append(containers, c)
		}
		a.mu.Unlock()
		var gpus map[string]*hostproto.GPUSample
		var samples []*hostproto.ContainerSample
		for _, c := range containers {
			source := c.usage.Load()
			if source == nil || c.hasExited() {
				continue
			}
			if len(source.gpus) > 0 && gpus == nil {
				gpus = a.readGPUs(ctx)
			}
			if s := source.sample(c.id, time.Now(), a.topology, gpus); s != nil {
				samples = append(samples, s)
			}
		}
		a.metrics.sampling.Observe(time.Since(began).Seconds())
		a.metrics.samples.Add(float64(len(samples)))
		if len(samples) > 0 {
			a.reportIfRoom(&hostproto.HostMessage{Body: &hostproto.HostMessage_Metrics{Metrics: &hostproto.ContainerMetrics{Samples: samples}}})
		}
	}
}

// sample reads the counters and returns their change since the previous
// sample, with CPU time counted on cores; the first reading only primes
// them. A container whose files are gone has exited and returns nil.
func (s *usageSource) sample(id string, now time.Time, topology cpu.Topology, gpus map[string]*hostproto.GPUSample) *hostproto.ContainerSample {
	usage, ok := readKeyed(filepath.Join(s.cgroupDir, "cpu.stat"), "usage_usec")
	if !ok {
		return nil
	}
	mem := readKeyedAll(filepath.Join(s.cgroupDir, "memory.stat"))
	swap, _ := readNumber(filepath.Join(s.cgroupDir, "memory.swap.current"))
	diskRead, diskWrite := readIOStat(filepath.Join(s.cgroupDir, "io.stat"))
	netRx, netTx := readNetDev(filepath.Join("/proc", strconv.Itoa(s.pid), "net", "dev"))
	current := &usageCounters{at: now, cpuUsec: usage, diskRead: diskRead, diskWrit: diskWrite, netRx: netRx, netTx: netTx}
	prev := s.prev
	s.prev = current
	if prev == nil {
		return nil
	}
	elapsed := now.Sub(prev.at).Milliseconds()
	if elapsed <= 0 {
		return nil
	}
	out := &hostproto.ContainerSample{
		ContainerId:     id,
		IntervalMs:      uint32(min(elapsed, 1<<31)), //nolint:gosec // Bounded above.
		CpuUsageUsec:    topology.CPUTime(delta(current.cpuUsec, prev.cpuUsec)),
		MemoryRssBytes:  mem["anon"] + mem["file_mapped"],
		MemorySwapBytes: swap,
		NetworkRxBytes:  delta(current.netRx, prev.netRx),
		NetworkTxBytes:  delta(current.netTx, prev.netTx),
		DiskReadBytes:   delta(current.diskRead, prev.diskRead),
		DiskWriteBytes:  delta(current.diskWrit, prev.diskWrit),
	}
	for _, uuid := range s.gpus {
		if g, ok := gpus[uuid]; ok {
			out.Gpus = append(out.Gpus, g)
		}
	}
	return out
}

// delta is the counter's growth; a counter that went backwards was reset.
func delta(current, prev uint64) uint64 {
	if current < prev {
		return current
	}
	return current - prev
}

func readNumber(path string) (uint64, bool) {
	data, err := os.ReadFile(path) //nolint:gosec // A cgroup file the agent located.
	if err != nil {
		return 0, false
	}
	v, err := strconv.ParseUint(strings.TrimSpace(string(data)), 10, 64)
	return v, err == nil
}

// readKeyedAll parses "key value" lines.
func readKeyedAll(path string) map[string]uint64 {
	out := map[string]uint64{}
	data, err := os.ReadFile(path) //nolint:gosec // A cgroup file the agent located.
	if err != nil {
		return out
	}
	for line := range strings.SplitSeq(string(data), "\n") {
		key, value, ok := strings.Cut(line, " ")
		if !ok {
			continue
		}
		if v, err := strconv.ParseUint(strings.TrimSpace(value), 10, 64); err == nil {
			out[key] = v
		}
	}
	return out
}

func readKeyed(path, key string) (uint64, bool) {
	values := readKeyedAll(path)
	v, ok := values[key]
	return v, ok
}

// readIOStat sums rbytes and wbytes over the cgroup's devices.
func readIOStat(path string) (read, write uint64) {
	data, err := os.ReadFile(path) //nolint:gosec // A cgroup file the agent located.
	if err != nil {
		return 0, 0
	}
	for field := range strings.FieldsSeq(string(data)) {
		key, value, ok := strings.Cut(field, "=")
		if !ok {
			continue
		}
		v, err := strconv.ParseUint(value, 10, 64)
		if err != nil {
			continue
		}
		switch key {
		case "rbytes":
			read += v
		case "wbytes":
			write += v
		}
	}
	return read, write
}

// readNetDev sums received and sent bytes over the container's network
// namespace, leaving out loopback.
func readNetDev(path string) (rx, tx uint64) {
	data, err := os.ReadFile(path) //nolint:gosec // The proc file of the container's process.
	if err != nil {
		return 0, 0
	}
	scanner := bufio.NewScanner(bytes.NewReader(data))
	for scanner.Scan() {
		name, counters, ok := strings.Cut(scanner.Text(), ":")
		if !ok || strings.TrimSpace(name) == "lo" {
			continue
		}
		fields := strings.Fields(counters)
		if len(fields) < 9 {
			continue
		}
		r, errR := strconv.ParseUint(fields[0], 10, 64)
		t, errT := strconv.ParseUint(fields[8], 10, 64)
		if errR == nil && errT == nil {
			rx += r
			tx += t
		}
	}
	return rx, tx
}

// readGPUs reads every GPU's use through NVML's nvidia-smi, keyed by UUID.
// A host without it returns none.
func (a *Agent) readGPUs(ctx context.Context) map[string]*hostproto.GPUSample {
	ctx, cancel := context.WithTimeout(ctx, gpuSampleTimeout)
	defer cancel()
	out := map[string]*hostproto.GPUSample{}
	data, err := exec.CommandContext(ctx, "nvidia-smi",
		"--query-gpu=uuid,name,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits").Output()
	if err != nil {
		a.log.Debug("reading GPU use failed", "error", err)
		return out
	}
	return parseGPUSamples(data)
}

func parseGPUSamples(data []byte) map[string]*hostproto.GPUSample {
	out := map[string]*hostproto.GPUSample{}
	for line := range strings.SplitSeq(string(data), "\n") {
		fields := strings.Split(line, ",")
		if len(fields) != 5 {
			continue
		}
		for i := range fields {
			fields[i] = strings.TrimSpace(fields[i])
		}
		util, _ := strconv.ParseUint(fields[2], 10, 32)
		used, _ := strconv.ParseUint(fields[3], 10, 64)
		total, _ := strconv.ParseUint(fields[4], 10, 64)
		out[fields[0]] = &hostproto.GPUSample{
			Uuid: fields[0], Name: fields[1], UtilizationPercent: uint32(util), //nolint:gosec // Percent.
			MemoryUsedBytes: used << 20, MemoryTotalBytes: total << 20,
		}
	}
	return out
}
