package edge

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
)

// InvokeWait is how long an invocation holds the request for its result.
const InvokeWait = 60 * time.Second

// ErrInvalidArguments rejects a body or query that cannot become arguments.
var ErrInvalidArguments = errors.New("invalid request payload")

// InvocationArguments turns an HTTP body and query into a function's JSON
// arguments: the body's `args` and `kwargs`
// when present, else the whole body as keyword arguments, plus each query
// parameter as a keyword argument, a float when it parses as one and a list
// when it repeats.
func InvocationArguments(body []byte, query url.Values) ([]byte, error) {
	args := []json.RawMessage{}
	kwargs := map[string]json.RawMessage{}
	if len(bytes.TrimSpace(body)) > 0 {
		var payload map[string]json.RawMessage
		if err := json.Unmarshal(body, &payload); err != nil {
			var other any
			if json.Unmarshal(body, &other) == nil {
				return nil, fmt.Errorf("%w: request payload must be a JSON object", ErrInvalidArguments)
			}
			return nil, ErrInvalidArguments
		}
		delete(payload, "result_format")
		if raw, ok := payload["args"]; ok {
			delete(payload, "args")
			var list []json.RawMessage
			if json.Unmarshal(raw, &list) == nil && list != nil {
				args = list
			}
		}
		raw, hasKwargs := payload["kwargs"]
		delete(payload, "kwargs")
		var object map[string]json.RawMessage
		switch {
		case hasKwargs && json.Unmarshal(raw, &object) == nil && object != nil:
			kwargs = object
		case len(payload) > 0:
			kwargs = payload
		}
	}
	keys := make([]string, 0, len(query))
	for key := range query {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		values := query[key]
		if len(values) == 1 {
			kwargs[key] = queryValue(values[0])
			continue
		}
		list := make([]json.RawMessage, len(values))
		for n, v := range values {
			list[n] = queryValue(v)
		}
		encoded, err := json.Marshal(list)
		if err != nil {
			return nil, fmt.Errorf("encode query list: %w", err)
		}
		kwargs[key] = encoded
	}
	out, err := json.Marshal(map[string]any{"args": args, "kwargs": kwargs})
	if err != nil {
		return nil, fmt.Errorf("encode arguments: %w", err)
	}
	return out, nil
}

// queryValue is a float when the value parses as one, else the string.
func queryValue(value string) json.RawMessage {
	if f, err := strconv.ParseFloat(strings.TrimSpace(value), 64); err == nil && !math.IsInf(f, 0) && !math.IsNaN(f) {
		text := strconv.FormatFloat(f, 'g', -1, 64)
		if !strings.ContainsAny(text, ".e") {
			text += ".0"
		}
		return json.RawMessage(text)
	}
	encoded, _ := json.Marshal(value) //nolint:errchkjson // a string always encodes
	return encoded
}

// Invoke admits one JSON task against a function, on release when it is
// set, and waits up to wait for it. A succeeded task carries its JSON result.
func Invoke(ctx context.Context, exec *execution.Execution, listener *database.Listener, req execution.SubmitRequest, arguments []byte, wait time.Duration) (apitypes.Invocation, error) {
	req.Inputs = []execution.TaskInput{{Payload: execution.Payload{Encoding: execution.EncodingJSON, Data: arguments}}}
	tasks, err := exec.Submit(ctx, req)
	if err != nil {
		return apitypes.Invocation{}, err
	}
	task, err := exec.GetTask(ctx, listener, req.Workspace, tasks[0].ID, wait)
	if err != nil {
		return apitypes.Invocation{}, err
	}
	out := apitypes.Invocation{Task: execution.APITask(task)}
	if task.Status == execution.TaskSucceeded {
		result, err := exec.TaskResult(ctx, req.Workspace, task.ID)
		if err != nil {
			return apitypes.Invocation{}, err
		}
		if result.Encoding == execution.EncodingJSON {
			value := json.RawMessage(result.Data)
			out.Result = &value
		}
	}
	return out, nil
}

// invoke runs a function from its deployed host: POST with JSON arguments.
func (e *Edge) invoke(w http.ResponseWriter, r *http.Request, t target) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		writeError(w, http.StatusMethodNotAllowed, "a function is invoked with POST")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, execution.MaxPayloadBytes+1))
	if err != nil {
		e.fail(w, r, err)
		return
	}
	if len(body) > execution.MaxPayloadBytes {
		e.fail(w, r, errBodyTooLarge)
		return
	}
	arguments, err := InvocationArguments(body, r.URL.Query())
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	out, err := Invoke(r.Context(), e.execution, e.listener, execution.SubmitRequest{
		Workspace: t.workload.workspace, App: t.workload.app, Function: t.workload.name, Release: &t.release.id,
	}, arguments, InvokeWait)
	var tooMany *execution.TooManyPendingError
	switch {
	case errors.As(err, &tooMany):
		writeError(w, http.StatusTooManyRequests, tooMany.Error())
		return
	case errors.Is(err, execution.ErrNotAccepting):
		writeError(w, http.StatusConflict, "the function is stopped or its app is paused")
		return
	case errors.Is(err, execution.ErrNotFound):
		writeError(w, http.StatusNotFound, "the function is not deployed")
		return
	case errors.Is(err, execution.ErrPayloadTooLarge):
		e.fail(w, r, errBodyTooLarge)
		return
	case err != nil:
		e.fail(w, r, err)
		return
	}
	// Function invocations are tasks; the header names it.
	w.Header().Set("X-Task-Id", out.Task.Id.String())
	w.Header().Add("Access-Control-Expose-Headers", "X-Task-Id")
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(out) //nolint:errchkjson // The client is gone if this fails.
}
