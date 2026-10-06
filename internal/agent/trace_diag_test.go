package agent

import (
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

// Temporary: why the host's snapshotter traced no reads on CI.
func TestTraceDiagnostic(t *testing.T) {
	client, err := layersource.Dial(layersource.Socket)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	layers := layersOf(imageLayers[testImage])
	probe := func(what string) {
		if err := client.StartTrace(t.Context(), "probe", layers); err != nil {
			t.Errorf("%s: start probe: %v", what, err)
			return
		}
		_, complete, err := client.EndTrace(t.Context(), "probe")
		t.Logf("%s: probe complete (no layer mounted) %v %v", what, complete, err)
	}
	probe("before the env")
	e := newEnv(t)
	e.startAgent()
	s := e.session()
	probe("after the agent started")
	start := e.startCommand("app:handle", 1)
	id := start.GetStart().GetContainerId()
	start.GetStart().RecordTrace = true
	s.send(t, start)
	s.phase(t, id, ready)
	time.Sleep(time.Second)
	reads, complete, err := client.EndTrace(t.Context(), id)
	t.Logf("at ready: %d reads, complete %v, %v", len(reads), complete, err)
	mountinfo, _ := os.ReadFile("/proc/self/mountinfo")
	fuse := 0
	for line := range strings.SplitSeq(string(mountinfo), "\n") {
		if strings.Contains(line, "fuse") {
			fuse++
			t.Logf("mount: %s", line)
		}
	}
	t.Logf("at ready: %d fuse mounts; grants %v", fuse, layers)
	inspect, _ := exec.CommandContext(t.Context(), "docker", "inspect", "--format", "{{.Driver}} {{json .GraphDriver}} {{.Image}}", "lazycloud-"+id).CombinedOutput()
	t.Logf("container: %s", inspect)
	image, _ := exec.CommandContext(t.Context(), "docker", "image", "inspect", "--format", "{{json .RootFS}}", testImage).CombinedOutput()
	t.Logf("image: %s", image)
	out, _ := exec.CommandContext(t.Context(), "sudo", "journalctl", "-u", "lazycloud-snapshotter", "-n", "40", "--no-pager").CombinedOutput()
	t.Logf("snapshotter: %s", out)
	s.send(t, stopCommand(id, 0))
	s.phase(t, id, exited)
	time.Sleep(2 * time.Second)
	probe("after the container exited")
	e.removeContainers()
	time.Sleep(2 * time.Second)
	probe("after the containers were removed")
	t.Error("diagnostic")
}
