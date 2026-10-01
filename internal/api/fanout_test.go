package api_test

import (
	"encoding/json"
	"net/http"
	"os"
	"runtime"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// TestMeasureChangeFanout opens 1,000 change streams on one workspace and
// times how long one committed change takes to reach each of them. Set
// LAZYCLOUD_MEASURE=1 to run it.
func TestMeasureChangeFanout(t *testing.T) {
	if os.Getenv("LAZYCLOUD_MEASURE") == "" {
		t.Skip("set LAZYCLOUD_MEASURE=1 to measure")
	}
	const streams = 1000
	e := newEnv(t)
	e.deploy()
	http.DefaultTransport.(*http.Transport).MaxIdleConnsPerHost = streams

	runtime.GC()
	var before runtime.MemStats
	runtime.ReadMemStats(&before)
	goroutines := runtime.NumGoroutine()
	all := make([]<-chan sseEvent, streams)
	for i := range all {
		resp, events := e.openStream(e.owner, "")
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("stream %d: %d", i, resp.StatusCode)
		}
		all[i] = events
	}
	runtime.GC()
	var after runtime.MemStats
	runtime.ReadMemStats(&after)
	t.Logf("%d streams: %d goroutines (client and server), heap %.1f KiB per stream",
		streams, runtime.NumGoroutine()-goroutines, float64(after.HeapAlloc-before.HeapAlloc)/streams/1024)

	fnPath := "/v1/workspaces/acme/apps/reports/functions/summarize_sales/tasks"
	raw := json.RawMessage(`{"args": [], "kwargs": {}}`)
	submit := apitypes.SubmitTasksRequest{Inputs: []apitypes.TaskInput{{Encoding: apitypes.TaskInputEncodingJson, Value: &raw}}}
	for round := range 3 {
		began := time.Now()
		if status := e.do("POST", fnPath, e.owner, submit, nil); status != 201 {
			t.Fatalf("submit: %d", status)
		}
		committed := time.Since(began)
		latencies := make([]time.Duration, streams)
		var wg sync.WaitGroup
		for i, events := range all {
			wg.Go(func() {
				for ev := range events {
					if ev.event == "change" {
						latencies[i] = time.Since(began)
						return
					}
				}
			})
		}
		wg.Wait()
		slices.Sort(latencies)
		t.Logf("round %d: submit returned in %s; delivered to %d streams p50 %s p99 %s max %s",
			round, committed, streams, latencies[streams/2], latencies[streams*99/100], latencies[streams-1])
	}
}
