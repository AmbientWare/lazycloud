package agent

import (
	"fmt"
	"os"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// TestMeasureMounts times the volume stage of starts with 0, 1 and 3
// mounters and their release after the container exits. It fails on
// purpose so that CI prints the numbers.
func TestMeasureMounts(t *testing.T) {
	geesefs := testGeeseFS(t)
	store := newTestStore(t)
	e := newEnv(t)
	t.Cleanup(e.stopMounts)
	e.geesefs = geesefs
	e.startAgent()
	s := e.session()
	source := serveSource(t, "testdata/volumes")
	endpoints := []string{store.endpoint, strings.Replace(store.endpoint, "127.0.0.1", "localhost", 1), strings.Replace(store.endpoint, "127.0.0.1", "LOCALHOST", 1)}
	bucket := func(endpoint, path string) *hostproto.VolumeMount {
		return &hostproto.VolumeMount{
			MountPath: path, ReadOnly: true,
			Source: &hostproto.VolumeMount_CloudBucket{CloudBucket: &hostproto.CloudBucket{
				Bucket: store.bucket, Prefix: "measure/", Region: store.region, Endpoint: endpoint, ForcePathStyle: true,
				AccessKeyId: store.accessKey, SecretAccessKey: store.secretKey,
			}},
		}
	}
	var report []string
	for _, n := range []int{0, 1, 3, 0, 1, 3, 0, 1, 3, 0, 1, 3, 0, 1, 3, 0, 1, 3} {
		start := e.startCommand("app:handle", 1)
		start.GetStart().Source = source
		for j := range n {
			start.GetStart().Volumes = append(start.GetStart().Volumes, bucket(endpoints[j], fmt.Sprintf("/m%d", j)))
		}
		reserveMounters(start, int64(n))
		id := start.GetStart().GetContainerId()
		sent := time.Now()
		s.send(t, start)
		r := s.phase(t, id, ready)
		total := time.Since(sent)
		var create time.Duration
		for _, stage := range r.GetStartup() {
			if stage.GetKind() == hostproto.StartupStageKind_STARTUP_STAGE_KIND_CREATE {
				create = stage.GetFinishedAt().AsTime().Sub(stage.GetStartedAt().AsTime())
			}
		}
		if got := len(e.mounters(false, id)); got != n {
			t.Fatalf("%d mounters run, want %d", got, n)
		}
		s.send(t, stopCommand(id, 1))
		s.phase(t, id, exited)
		exitedAt := time.Now()
		for {
			_, err := os.Stat(e.sliceDir(id))
			if len(e.mounters(true, id)) == 0 && os.IsNotExist(err) {
				break
			}
			if time.Since(exitedAt) > time.Minute {
				t.Fatal("not released")
			}
			time.Sleep(10 * time.Millisecond)
		}
		release := time.Since(exitedAt)
		report = append(report, fmt.Sprintf("MEASURE mounters=%d create_ms=%d ready_ms=%d release_ms=%d", n, create.Milliseconds(), total.Milliseconds(), release.Milliseconds()))
	}
	slices.Sort(report)
	t.Errorf("\n%s", strings.Join(report, "\n"))
}
