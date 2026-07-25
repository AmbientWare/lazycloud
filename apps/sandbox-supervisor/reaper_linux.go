//go:build linux

package main

import (
	"bytes"
	"os"
	"strconv"
	"syscall"
	"time"
)

func (s *supervisor) reapAdoptedChildren() {
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	for range ticker.C {
		entries, err := os.ReadDir("/proc")
		if err != nil {
			continue
		}
		for _, entry := range entries {
			pid, err := strconv.Atoi(entry.Name())
			if err != nil || s.isDirectChild(pid) {
				continue
			}
			status, err := os.ReadFile("/proc/" + entry.Name() + "/status")
			if err != nil || processParentPID(status) != os.Getpid() {
				continue
			}
			var waitStatus syscall.WaitStatus
			_, _ = syscall.Wait4(pid, &waitStatus, syscall.WNOHANG, nil)
		}
	}
}

func processParentPID(status []byte) int {
	for _, line := range bytes.Split(status, []byte{'\n'}) {
		if !bytes.HasPrefix(line, []byte("PPid:")) {
			continue
		}
		pid, _ := strconv.Atoi(string(bytes.TrimSpace(bytes.TrimPrefix(line, []byte("PPid:")))))
		return pid
	}
	return 0
}
