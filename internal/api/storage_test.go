package api_test

import (
	"encoding/json"
	"net/http"
	"regexp"
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/api"
	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// TestSchemaPatternsCompile guards against patterns Go's regexp rejects,
// such as repeat counts above 1000, which only fail when a request uses them.
func TestSchemaPatternsCompile(t *testing.T) {
	spec, err := api.GetSwagger()
	if err != nil {
		t.Fatal(err)
	}
	for name, schema := range spec.Components.Schemas {
		if p := schema.Value.Pattern; p != "" {
			if _, err := regexp.Compile(p); err != nil {
				t.Errorf("schema %s: %v", name, err)
			}
		}
	}
}

// TestStorageRoutes checks the public storage contract end to end through
// validation, authorization and error mapping: names and keys with slashes
// travel escaped, absence differs from errors, and typed errors keep their
// codes.
func TestStorageRoutes(t *testing.T) {
	e := newEnv(t)
	ws := "/v1/workspaces/acme"
	var failure apitypes.Error

	if status := e.do("POST", ws+"/queues/jobs%2Fnightly/messages", e.outsider, apitypes.PutQueueMessagesRequest{Messages: [][]byte{[]byte("1")}}, nil); status != http.StatusForbidden {
		t.Fatalf("put by an outsider: %d", status)
	}
	if status := e.do("POST", ws+"/queues/jobs%2Fnightly/messages", e.owner, apitypes.PutQueueMessagesRequest{Messages: [][]byte{[]byte(`"a"`), []byte(`"b"`)}}, nil); status != http.StatusNoContent {
		t.Fatalf("put: %d", status)
	}
	var popped apitypes.QueueMessageResult
	if status := e.do("POST", ws+"/queues/jobs%2Fnightly/pop", e.owner, nil, &popped); status != http.StatusOK || popped.Message == nil || string(*popped.Message) != `"a"` {
		t.Fatalf("pop: %d %+v", status, popped)
	}
	var queues apitypes.QueuePage
	if status := e.do("GET", ws+"/queues", e.owner, nil, &queues); status != http.StatusOK || len(queues.Queues) != 1 || queues.Queues[0].Name != "jobs/nightly" || queues.Queues[0].Size != 1 {
		t.Fatalf("queues: %d %+v", status, queues)
	}
	var empty map[string]any
	if status := e.do("POST", ws+"/queues/never/pop", e.owner, nil, &empty); status != http.StatusOK || len(empty) != 0 {
		t.Fatalf("pop of an empty queue: %d %v", status, empty)
	}

	key := ws + "/maps/cache/entries/" + strings.ReplaceAll("user/42", "/", "%2F")
	var written apitypes.MapEntryWrite
	if status := e.do("PUT", key, e.owner, apitypes.SetMapEntryRequest{Value: []byte(`{"n":1}`)}, &written); status != http.StatusOK {
		t.Fatalf("set: %d", status)
	}
	var entry apitypes.MapEntry
	if status := e.do("GET", key, e.owner, nil, &entry); status != http.StatusOK || entry.Key != "user/42" || entry.Revision != written.Revision {
		t.Fatalf("get: %d %+v", status, entry)
	}
	stale := "999999"
	if status := e.do("PUT", key, e.owner, apitypes.SetMapEntryRequest{Value: []byte(`2`), IfRevision: &stale}, &failure); status != http.StatusConflict || failure.Code != apitypes.Conflict {
		t.Fatalf("stale write: %d %+v", status, failure)
	}
	ttl := 700000
	if status := e.do("PUT", key, e.owner, apitypes.SetMapEntryRequest{Value: []byte(`2`), TtlSeconds: &ttl}, &failure); status != http.StatusBadRequest {
		t.Fatalf("ttl above 7 days: %d", status)
	}
	if status := e.do("GET", ws+"/maps/cache/entries/missing", e.owner, nil, &failure); status != http.StatusNotFound || failure.Code != apitypes.NotFound {
		t.Fatalf("missing key: %d %+v", status, failure)
	}
	var keys apitypes.MapKeyPage
	if status := e.do("GET", ws+"/maps/cache/keys?prefix=user%2F", e.owner, nil, &keys); status != http.StatusOK || len(keys.Keys) != 1 {
		t.Fatalf("keys: %d %+v", status, keys)
	}

	if status := e.do("POST", ws+"/volumes", e.owner, apitypes.CreateVolumeRequest{Name: "data"}, nil); status != http.StatusOK {
		t.Fatalf("create volume: %d", status)
	}
	if status := e.do("DELETE", ws+"/volumes/data/files?path=.", e.owner, nil, &failure); status != http.StatusBadRequest || failure.Code != apitypes.InvalidRequest {
		t.Fatalf("remove the volume root: %d %+v", status, failure)
	}
	if status := e.do("GET", ws+"/volumes/data/files/stat?path=..%2Fother", e.owner, nil, &failure); status != http.StatusBadRequest {
		t.Fatalf("stat outside the volume: %d", status)
	}
	if status := e.do("POST", ws+"/volumes", e.owner, apitypes.CreateVolumeRequest{Name: "has/slash"}, nil); status != http.StatusBadRequest {
		t.Fatalf("volume name with a slash: %d", status)
	}
	if status := e.do("DELETE", ws+"/volumes/data", e.owner, nil, nil); status != http.StatusNoContent {
		t.Fatalf("delete volume: %d", status)
	}
	if status := e.do("DELETE", ws+"/volumes/data", e.owner, nil, nil); status != http.StatusNotFound {
		t.Fatalf("delete a deleted volume: %d", status)
	}

	if status := e.do("POST", ws+"/artifacts", e.owner, apitypes.CreateArtifactRequest{TaskId: uuid.New(), Filename: "a.txt", SizeBytes: 1}, nil); status != http.StatusNotFound {
		t.Fatalf("artifact for an unknown task: %d", status)
	}
	if status := e.do("POST", ws+"/artifacts", e.owner, json.RawMessage(`{"task_id":"`+uuid.NewString()+`","filename":"a/b","size_bytes":1}`), nil); status != http.StatusBadRequest {
		t.Fatalf("artifact name with a slash: %d", status)
	}
	if status := e.do("DELETE", ws+"/disks/root", e.owner, nil, nil); status != http.StatusNotFound {
		t.Fatalf("delete a missing disk: %d", status)
	}
}
