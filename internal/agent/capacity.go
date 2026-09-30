package agent

import (
	"bufio"
	"bytes"
	"fmt"
	"os"
	"runtime"
	"strconv"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// The host keeps a reserve for the agent, Docker and the kernel: the larger
// of a fixed floor and a tenth of the machine.
const (
	reserveCPUMillis   = 500
	reserveMemoryBytes = 512 << 20
)

func detectCapacity() (*hostproto.Capacity, error) {
	cpu := int64(runtime.NumCPU()) * 1000
	memory, err := memTotal()
	if err != nil {
		return nil, err
	}
	return &hostproto.Capacity{
		CpuMillis:   max(0, cpu-max(reserveCPUMillis, cpu/10)),
		MemoryBytes: max(0, memory-max(reserveMemoryBytes, memory/10)),
	}, nil
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
	return 0, fmt.Errorf("no MemTotal in /proc/meminfo")
}
