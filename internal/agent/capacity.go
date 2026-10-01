package agent

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"math"
	"os"
	"os/exec"
	"regexp"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// The host keeps a reserve for the agent, Docker and the kernel: the larger
// of a fixed floor and a tenth of the machine. An explicit limit replaces the
// reserve.
const (
	reserveCPUMillis   = 500
	reserveMemoryBytes = 512 << 20
)

// gpuQueryTimeout bounds nvidia-smi; a hung driver means no GPUs.
const gpuQueryTimeout = 5 * time.Second

// Preflight check severities.
const (
	severityInfo  = "info"
	severityError = "error"
)

// Limits caps what the host offers. Zero values offer what was detected.
type Limits struct {
	CPUMillis   int64
	MemoryBytes int64
	// GPUs offers the first GPUs detected, none when zero; nil offers all.
	// GPUIDs names them instead.
	GPUs   *int
	GPUIDs []string
}

// gpuDevice is one NVIDIA GPU as nvidia-smi reports it.
type gpuDevice struct {
	Index string
	UUID  string
	Model string
}

// detected is the machine before limits.
type detected struct {
	cpuMillis   int64
	memoryBytes int64
	gpus        []gpuDevice
}

func detect(ctx context.Context) (detected, error) {
	memory, err := memTotal()
	if err != nil {
		return detected{}, err
	}
	return detected{cpuMillis: int64(runtime.NumCPU()) * 1000, memoryBytes: memory, gpus: detectGPUs(ctx)}, nil
}

// offer is what the host offers after its limits, with the checks that
// decided it.
type offer struct {
	capacity *hostproto.Capacity
	gpus     []gpuDevice
	checks   []*hostproto.PreflightCheck
}

// failed returns the failed error-severity checks.
func (o offer) failed() []*hostproto.PreflightCheck {
	var failed []*hostproto.PreflightCheck
	for _, check := range o.checks {
		if !check.GetOk() && check.GetSeverity() == severityError {
			failed = append(failed, check)
		}
	}
	return failed
}

// resolveOffer applies limits to the detected machine. A limit the machine
// cannot meet is a failed check, so the server shows why the host cannot
// join.
func resolveOffer(machine detected, limits Limits) offer {
	o := offer{capacity: &hostproto.Capacity{
		CpuMillis:   max(0, machine.cpuMillis-max(reserveCPUMillis, machine.cpuMillis/10)),
		MemoryBytes: max(0, machine.memoryBytes-max(reserveMemoryBytes, machine.memoryBytes/10)),
	}}
	if limits.CPUMillis > 0 {
		ok := limits.CPUMillis <= machine.cpuMillis
		message := "requested CPU exceeds detected CPU"
		if ok {
			o.capacity.CpuMillis = limits.CPUMillis
			message = fmt.Sprintf("using %dm CPU", limits.CPUMillis)
		}
		o.checks = append(o.checks, check("capacity.max_cpu", ok, message, "lower --max-cpu to at most the host's cores"))
	} else {
		o.checks = append(o.checks, check("capacity.max_cpu", true, fmt.Sprintf("using %dm CPU", o.capacity.GetCpuMillis()), ""))
	}
	if limits.MemoryBytes > 0 {
		ok := limits.MemoryBytes <= machine.memoryBytes
		message := "requested memory exceeds detected memory"
		if ok {
			o.capacity.MemoryBytes = limits.MemoryBytes
			message = fmt.Sprintf("using %d MB memory", limits.MemoryBytes>>20)
		}
		o.checks = append(o.checks, check("capacity.max_memory", ok, message, "lower --max-memory to at most the host's memory"))
	} else {
		o.checks = append(o.checks, check("capacity.max_memory", true, fmt.Sprintf("using %d MB memory", o.capacity.GetMemoryBytes()>>20), ""))
	}

	gpus, gpuCheck := selectGPUs(machine.gpus, limits)
	if gpuCheck != nil {
		o.checks = append(o.checks, gpuCheck)
	}
	if len(gpus) == 0 {
		return o
	}
	models := []string{}
	for _, gpu := range gpus {
		if !slices.Contains(models, gpu.Model) {
			models = append(models, gpu.Model)
		}
	}
	if len(models) > 1 {
		o.checks = append(o.checks, check("gpu.model", false,
			"the offered GPUs are of mixed models: "+strings.Join(models, ", "),
			"offer GPUs of one model with --gpu-ids"))
		return o
	}
	if !nvidiaRuntimeInstalled() {
		o.checks = append(o.checks, check("nvidia-runtime", false, "the NVIDIA container runtime is not installed",
			"install the NVIDIA Container Toolkit and run nvidia-ctk runtime configure --runtime=docker"))
		return o
	}
	o.checks = append(o.checks, check("nvidia-runtime", true, "the NVIDIA container runtime is installed", ""))
	o.gpus = gpus
	o.capacity.GpuType = models[0]
	o.capacity.GpuCount = int32(len(gpus)) //nolint:gosec // a host has few GPUs
	return o
}

// selectGPUs picks the offered GPUs and the check that explains the choice.
func selectGPUs(gpus []gpuDevice, limits Limits) ([]gpuDevice, *hostproto.PreflightCheck) {
	switch {
	case len(limits.GPUIDs) > 0:
		selected := make([]gpuDevice, 0, len(limits.GPUIDs))
		for _, id := range limits.GPUIDs {
			i := slices.IndexFunc(gpus, func(g gpuDevice) bool { return g.Index == id || g.UUID == id })
			if i < 0 {
				return nil, check("capacity.gpu_ids", false, fmt.Sprintf("GPU id '%s' was not detected", id), "list GPUs with nvidia-smi -L")
			}
			if !slices.Contains(selected, gpus[i]) {
				selected = append(selected, gpus[i])
			}
		}
		return selected, check("capacity.gpu_ids", true, fmt.Sprintf("using %d GPUs", len(selected)), "")
	case limits.GPUs != nil:
		n := *limits.GPUs
		if n > len(gpus) {
			return nil, check("capacity.max_gpus", false, fmt.Sprintf("requested %d GPUs, detected %d", n, len(gpus)), "lower --max-gpus")
		}
		return gpus[:n], check("capacity.max_gpus", true, fmt.Sprintf("using %d GPUs", n), "")
	case len(gpus) > 0:
		return gpus, check("capacity.max_gpus", true, fmt.Sprintf("using %d GPUs", len(gpus)), "")
	}
	return nil, nil
}

func check(name string, ok bool, message, remediation string) *hostproto.PreflightCheck {
	c := &hostproto.PreflightCheck{Name: name, Ok: ok, Message: message, Severity: severityInfo}
	if !ok {
		c.Severity = severityError
		c.Remediation = remediation
	}
	return c
}

// detectGPUs lists NVIDIA GPUs. A host without nvidia-smi, or one where it
// fails, has none.
func detectGPUs(ctx context.Context) []gpuDevice {
	if _, err := exec.LookPath("nvidia-smi"); err != nil {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, gpuQueryTimeout)
	defer cancel()
	out, err := exec.CommandContext(ctx, "nvidia-smi", "--query-gpu=index,uuid,name", "--format=csv,noheader").Output()
	if err != nil {
		return nil
	}
	return parseGPUs(string(out))
}

func parseGPUs(output string) []gpuDevice {
	var gpus []gpuDevice
	for line := range strings.Lines(output) {
		fields := strings.SplitN(line, ",", 3)
		if len(fields) != 3 {
			continue
		}
		gpus = append(gpus, gpuDevice{
			Index: strings.TrimSpace(fields[0]),
			UUID:  strings.TrimSpace(fields[1]),
			Model: gpuModel(fields[2]),
		})
	}
	return gpus
}

func nvidiaRuntimeInstalled() bool {
	for _, name := range []string{"nvidia-ctk", "nvidia-container-runtime"} {
		if _, err := exec.LookPath(name); err == nil {
			return true
		}
	}
	return false
}

// gpuAliases maps compact names to the platform's model vocabulary, most
// specific first.
var gpuAliases = [...]struct{ alias, model string }{ //nolint:gochecknoglobals // constant table
	{"GH200", "GH200"}, {"H200", "H200"}, {"H100", "H100"},
	{"L40S", "L40S"}, {"L40", "L40"}, {"L4", "L4"}, {"T4", "T4"}, {"A10G", "A10G"},
}

var nonAlphanumeric = regexp.MustCompile(`[^A-Z0-9]+`)

// gpuModel normalizes an nvidia-smi name to the platform's model names. A
// card outside that vocabulary keeps its compact name, such as RTX3090.
func gpuModel(name string) string {
	key := nonAlphanumeric.ReplaceAllString(strings.ToUpper(strings.TrimSpace(name)), "")
	for _, vendor := range []string{"NVIDIA", "GEFORCE", "TESLA", "QUADRO"} {
		key = strings.ReplaceAll(key, vendor, "")
	}
	if strings.Contains(key, "A100") {
		switch {
		case strings.Contains(key, "80G"):
			return "A100-80"
		case strings.Contains(key, "40G"):
			return "A100-40"
		}
	}
	for _, a := range gpuAliases {
		if strings.Contains(key, a.alias) {
			return a.model
		}
	}
	return key
}

// ParseCPU reads a core count such as 2 or 1.5 as millicores.
func ParseCPU(value string) (int64, error) {
	cores, err := strconv.ParseFloat(strings.TrimSpace(value), 64)
	if err != nil || cores <= 0 || math.IsInf(cores, 0) {
		return 0, errors.New("max cpu must be a positive number of cores")
	}
	return int64(math.Floor(cores*1000 + 0.5)), nil
}

// memoryUnits are suffixes and their size in MB (MiB), longest first, as in
// the reference: decimal suffixes count 1000 MB per GB. A bare number is MB.
var memoryUnits = [...]struct { //nolint:gochecknoglobals // constant table
	suffix string
	mb     float64
}{
	{"tib", 1 << 20}, {"tb", 1e6}, {"gib", 1 << 10}, {"gb", 1e3}, {"gi", 1 << 10}, {"g", 1e3},
	{"mib", 1}, {"mb", 1}, {"mi", 1}, {"m", 1},
}

// ParseMemory reads a size such as 16gib, 512mb or 2048 (MB) as bytes.
func ParseMemory(value string) (int64, error) {
	text := strings.ToLower(strings.TrimSpace(value))
	scale := 1.0
	for _, unit := range memoryUnits {
		if strings.HasSuffix(text, unit.suffix) {
			text, scale = strings.TrimSpace(strings.TrimSuffix(text, unit.suffix)), unit.mb
			break
		}
	}
	amount, err := strconv.ParseFloat(text, 64)
	if err != nil || amount <= 0 || math.IsInf(amount, 0) {
		return 0, errors.New("max memory must be a positive size")
	}
	return int64(math.Floor(amount*scale+0.5)) << 20, nil
}

func memTotal() (int64, error) {
	data, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return 0, fmt.Errorf("read memory size: %w", err)
	}
	scanner := bufio.NewScanner(bytes.NewReader(data))
	for scanner.Scan() {
		fields := bytes.Fields(scanner.Bytes())
		if len(fields) >= 2 && string(fields[0]) == "MemTotal:" {
			kib, err := strconv.ParseInt(string(fields[1]), 10, 64)
			if err != nil {
				return 0, fmt.Errorf("parse MemTotal: %w", err)
			}
			return kib << 10, nil
		}
	}
	return 0, errors.New("no MemTotal in /proc/meminfo")
}
