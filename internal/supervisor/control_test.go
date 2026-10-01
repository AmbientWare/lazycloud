package supervisor

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes" //nolint:depguard // the control API bodies are the public schemas
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// controlHarness serves a control API on a temporary socket with a
// temporary workspace.
type controlHarness struct {
	t         *testing.T
	ctl       *control
	socket    string
	workspace string
	client    *http.Client
}

func startControl(t *testing.T, sshIdentity *hostproto.SshServer) *controlHarness {
	t.Helper()
	dir, err := os.MkdirTemp("", "lcctl")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	ctl, err := newControl(slog.New(slog.NewTextHandler(os.Stderr, nil)), newChildren(), sshIdentity)
	if err != nil {
		t.Fatal(err)
	}
	ctl.workspace = filepath.Join(dir, "workspace")
	if err := os.Mkdir(ctl.workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	socket := filepath.Join(dir, "control.sock")
	if err := ctl.listen(t.Context(), socket); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(ctl.close)
	return &controlHarness{t: t, ctl: ctl, socket: socket, workspace: ctl.workspace, client: unixClient(socket)}
}

func unixClient(socket string) *http.Client {
	return &http.Client{Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		},
	}}
}

// do sends a request and decodes a JSON answer into out when out is set.
func (h *controlHarness) do(method, target string, body any, out any) (int, http.Header) {
	h.t.Helper()
	var reader io.Reader
	switch b := body.(type) {
	case nil:
	case []byte:
		reader = bytes.NewReader(b)
	default:
		data, err := json.Marshal(b)
		if err != nil {
			h.t.Fatal(err)
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(h.t.Context(), method, "http://control"+target, reader)
	if err != nil {
		h.t.Fatal(err)
	}
	resp, err := h.client.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		h.t.Fatal(err)
	}
	if out != nil {
		if raw, ok := out.(*[]byte); ok {
			*raw = data
		} else if err := json.Unmarshal(data, out); err != nil {
			h.t.Fatalf("%s %s: decode %q: %v", method, target, data, err)
		}
	}
	return resp.StatusCode, resp.Header
}

// expectError checks a refusal's status and code.
func (h *controlHarness) expectError(method, target string, body any, status int, code apitypes.ErrorCode) {
	h.t.Helper()
	var e apitypes.Error
	if got, _ := h.do(method, target, body, &e); got != status || e.Code != code {
		h.t.Fatalf("%s %s: %d %+v, want %d %s", method, target, got, e, status, code)
	}
}

func (h *controlHarness) start(args []string, env map[string]string) apitypes.Process {
	h.t.Helper()
	req := apitypes.ProcessRequest{Args: args}
	if env != nil {
		req.Env = &env
	}
	var p apitypes.Process
	if status, _ := h.do(http.MethodPost, "/processes", req, &p); status != http.StatusCreated {
		h.t.Fatalf("start %v: %d", args, status)
	}
	return p
}

func (h *controlHarness) wait(id string) apitypes.Process {
	h.t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		var p apitypes.Process
		if status, _ := h.do(http.MethodGet, "/processes/"+id+"?wait_seconds=5", nil, &p); status != http.StatusOK {
			h.t.Fatalf("get process %s: %d", id, status)
		}
		if !p.Running {
			return p
		}
	}
	h.t.Fatalf("process %s did not exit", id)
	return apitypes.Process{}
}

func TestProcessesRunDirectlyWithEnvironmentAndExitCode(t *testing.T) {
	h := startControl(t, nil)
	t.Setenv("WR_CONTAINER_VALUE", "from-container")
	h.ctl.env = containerEnvironment()
	started := h.start([]string{"sh", "-c", `echo "$WR_CONTAINER_VALUE $WR_REQUEST_VALUE $(pwd)"; echo oops >&2; exit 3`},
		map[string]string{"WR_REQUEST_VALUE": "from-request"})
	if !started.Running || started.Pid == 0 || started.ExitCode != nil {
		t.Fatalf("started %+v", started)
	}
	p := h.wait(started.ProcessId)
	want := "from-container from-request " + h.workspace + "\n"
	if p.ExitCode == nil || *p.ExitCode != 3 || p.Stdout != want || p.Stderr != "oops\n" || p.ExpiresAt == nil {
		t.Fatalf("exited %+v, want stdout %q", p, want)
	}
	if p.StdoutTruncated || p.StderrTruncated {
		t.Fatalf("truncated %+v", p)
	}
	// Arguments are not interpreted by a shell.
	literal := h.wait(h.start([]string{"echo", "$HOME", "a;b"}, nil).ProcessId)
	if literal.Stdout != "$HOME a;b\n" {
		t.Fatalf("argv echo %q", literal.Stdout)
	}
	var list apitypes.ProcessList
	h.do(http.MethodGet, "/processes", nil, &list)
	if len(list.Processes) != 2 || list.Processes[0].Pid > list.Processes[1].Pid || list.Processes[0].ExitCode == nil {
		t.Fatalf("list %+v", list)
	}
}

func TestKillSignalsTheGroupAndAcceptsExitedProcesses(t *testing.T) {
	h := startControl(t, nil)
	// The child sleep shares the group, so it dies with sh and output ends.
	p := h.start([]string{"sh", "-c", "sleep 100 & wait"}, nil)
	if status, _ := h.do(http.MethodPost, "/processes/"+p.ProcessId+"/kill", nil, nil); status != http.StatusNoContent {
		t.Fatalf("kill: %d", status)
	}
	started := time.Now()
	exited := h.wait(p.ProcessId)
	if *exited.ExitCode != 128+15 || exited.StdoutTruncated || time.Since(started) > 900*time.Millisecond {
		t.Fatalf("after TERM %+v in %s", exited, time.Since(started))
	}
	if status, _ := h.do(http.MethodPost, "/processes/"+p.ProcessId+"/kill", apitypes.KillRequest{}, nil); status != http.StatusNoContent {
		t.Fatalf("kill exited: %d", status)
	}
	killed := h.start([]string{"sleep", "100"}, nil)
	sig := apitypes.KillRequestSignal("KILL")
	h.do(http.MethodPost, "/processes/"+killed.ProcessId+"/kill", apitypes.KillRequest{Signal: &sig}, nil)
	if got := h.wait(killed.ProcessId); *got.ExitCode != 128+9 {
		t.Fatalf("after KILL %+v", got)
	}
	h.expectError(http.MethodPost, "/processes/"+killed.ProcessId+"/kill", []byte(`{"signal":"STOP"}`), http.StatusBadRequest, apitypes.InvalidRequest)
	h.expectError(http.MethodPost, "/processes/nope/kill", nil, http.StatusNotFound, apitypes.NotFound)
	h.expectError(http.MethodGet, "/processes/nope", nil, http.StatusNotFound, apitypes.NotFound)
	h.expectError(http.MethodGet, "/processes/"+killed.ProcessId+"?wait_seconds=6", nil, http.StatusBadRequest, apitypes.InvalidRequest)
	h.expectError(http.MethodPost, "/processes", apitypes.ProcessRequest{Args: []string{"/no/such/program"}}, http.StatusBadRequest, apitypes.InvalidRequest)
	h.expectError(http.MethodPost, "/processes", apitypes.ProcessRequest{Args: []string{}}, http.StatusBadRequest, apitypes.InvalidRequest)
}

func TestGetProcessLongPollsUntilExit(t *testing.T) {
	h := startControl(t, nil)
	p := h.start([]string{"sleep", "0.3"}, nil)
	started := time.Now()
	var got apitypes.Process
	h.do(http.MethodGet, "/processes/"+p.ProcessId+"?wait_seconds=5", nil, &got)
	if got.Running || time.Since(started) > 2*time.Second {
		t.Fatalf("long poll %+v after %s", got, time.Since(started))
	}
	waiting := h.start([]string{"sleep", "10"}, nil)
	started = time.Now()
	h.do(http.MethodGet, "/processes/"+waiting.ProcessId+"?wait_seconds=0.2", nil, &got)
	if !got.Running || time.Since(started) < 200*time.Millisecond {
		t.Fatalf("bounded wait %+v after %s", got, time.Since(started))
	}
}

func TestProcessOutputIsCappedAndLateOutputMarksTruncated(t *testing.T) {
	h := startControl(t, nil)
	big := h.wait(h.start([]string{"sh", "-c", "head -c 300000 /dev/zero | tr '\\0' a"}, nil).ProcessId)
	if len(big.Stdout) != maxRetainedOutput || !big.StdoutTruncated || big.StderrTruncated {
		t.Fatalf("retained %d bytes, truncated %v/%v", len(big.Stdout), big.StdoutTruncated, big.StderrTruncated)
	}
	// A background child keeps the pipes open after the leader exits; the
	// result comes a second after the exit with the output marked
	// incomplete.
	p := h.start([]string{"sh", "-c", "sleep 5 & echo leader"}, nil)
	started := time.Now()
	late := h.wait(p.ProcessId)
	if late.Stdout != "leader\n" || !late.StdoutTruncated || !late.StderrTruncated || *late.ExitCode != 0 {
		t.Fatalf("late output %+v", late)
	}
	if elapsed := time.Since(started); elapsed < outputDrainGrace || elapsed > 3*time.Second {
		t.Fatalf("result after %s", elapsed)
	}
	t.Logf("exit to result with a descendant holding the pipes: %s", time.Since(started))
}

func TestFilesUploadDownloadStatAndList(t *testing.T) {
	h := startControl(t, nil)
	content := []byte("hello\nworld\n")
	if status, _ := h.do(http.MethodPut, "/files/content?path=nested/dir/a.txt&mode=384", content, nil); status != http.StatusNoContent {
		t.Fatalf("upload: %d", status)
	}
	info, err := os.Stat(filepath.Join(h.workspace, "nested/dir/a.txt"))
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("uploaded %v %v", info, err)
	}
	if parent, _ := os.Stat(filepath.Join(h.workspace, "nested")); parent.Mode().Perm() != 0o755 {
		t.Fatalf("parent mode %v", parent.Mode())
	}
	if entries, _ := os.ReadDir(filepath.Join(h.workspace, "nested/dir")); len(entries) != 1 {
		t.Fatalf("temporary files left: %v", entries)
	}
	var got []byte
	status, header := h.do(http.MethodGet, "/files/content?path="+filepath.Join(h.workspace, "nested/dir/a.txt"), nil, &got)
	if status != http.StatusOK || !bytes.Equal(got, content) || header.Get(TruncatedHeader) != "" {
		t.Fatalf("download %d %q %v", status, got, header)
	}
	status, header = h.do(http.MethodGet, "/files/content?path=nested/dir/a.txt&max_bytes=5&truncate=true", nil, &got)
	if status != http.StatusOK || string(got) != "hello" || header.Get(TruncatedHeader) != "true" {
		t.Fatalf("truncated download %d %q %v", status, got, header)
	}
	h.expectError(http.MethodGet, "/files/content?path=nested/dir/a.txt&max_bytes=5", nil, http.StatusRequestEntityTooLarge, apitypes.PayloadTooLarge)
	h.expectError(http.MethodGet, "/files/content?path=missing.txt", nil, http.StatusNotFound, apitypes.NotFound)
	h.expectError(http.MethodGet, "/files/content?path=nested", nil, http.StatusConflict, apitypes.Conflict)
	// A file without a size is read to tell whether it fits.
	status, _ = h.do(http.MethodGet, "/files/content?path=/proc/self/status&max_bytes=1&truncate=true", nil, &got)
	if status != http.StatusOK || len(got) != 1 {
		t.Fatalf("proc download %d %q", status, got)
	}

	var stat apitypes.ContainerFile
	h.do(http.MethodGet, "/files/stat?path=nested/dir/a.txt", nil, &stat)
	if stat.Name != "a.txt" || stat.Size != int64(len(content)) || stat.Permissions != 0o600 || stat.Mode&0o170000 != 0o100000 ||
		stat.IsDir || stat.Owner != strconv.Itoa(os.Getuid()) || stat.ModTime == nil {
		t.Fatalf("stat %+v", stat)
	}
	h.expectError(http.MethodGet, "/files/stat?path=missing", nil, http.StatusNotFound, apitypes.NotFound)

	for _, name := range []string{"c", "a", "b"} {
		h.do(http.MethodPut, "/files/content?path=list/"+name, []byte(name), nil)
	}
	var list apitypes.ContainerFileList
	h.do(http.MethodGet, "/files?path=list&limit=2", nil, &list)
	if len(list.Files) != 2 || list.Files[0].Name != "a" || list.Files[1].Name != "b" || !list.Truncated {
		t.Fatalf("limited list %+v", list)
	}
	h.do(http.MethodGet, "/files?path=list/c", nil, &list)
	if len(list.Files) != 1 || list.Files[0].Name != "c" || list.Truncated {
		t.Fatalf("file list %+v", list)
	}
	h.expectError(http.MethodGet, "/files?path=list&limit=10001", nil, http.StatusBadRequest, apitypes.InvalidRequest)
	h.expectError(http.MethodGet, "/files?path=", nil, http.StatusBadRequest, apitypes.InvalidRequest)
	h.expectError(http.MethodPut, "/files/content?path=list", []byte("x"), http.StatusConflict, apitypes.Conflict)
}

func TestDirectoriesCreateAndDelete(t *testing.T) {
	h := startControl(t, nil)
	if status, _ := h.do(http.MethodPost, "/directories?path=d/e&mode=448", nil, nil); status != http.StatusNoContent {
		t.Fatalf("create: %d", status)
	}
	if info, err := os.Stat(filepath.Join(h.workspace, "d/e")); err != nil || info.Mode().Perm() != 0o700 {
		t.Fatalf("created %v %v", info, err)
	}
	h.do(http.MethodPut, "/files/content?path=d/e/f.txt", []byte("x"), nil)
	h.expectError(http.MethodDelete, "/files?path=d", nil, http.StatusConflict, apitypes.Conflict)
	h.expectError(http.MethodDelete, "/directories?path=d/e/f.txt", nil, http.StatusConflict, apitypes.Conflict)
	h.expectError(http.MethodPost, "/directories?path=d/e/f.txt", nil, http.StatusConflict, apitypes.Conflict)
	for _, target := range []string{"/files?path=d/e/f.txt", "/files?path=d/e/f.txt", "/directories?path=d", "/directories?path=d"} {
		if status, _ := h.do(http.MethodDelete, target, nil, nil); status != http.StatusNoContent {
			t.Fatalf("delete %s: %d", target, status)
		}
	}
	if _, err := os.Stat(filepath.Join(h.workspace, "d")); !os.IsNotExist(err) {
		t.Fatalf("directory remains: %v", err)
	}
	h.expectError(http.MethodGet, "/nope", nil, http.StatusNotFound, apitypes.NotFound)
}

func TestFindAndReplaceEveryOccurrence(t *testing.T) {
	h := startControl(t, nil)
	files := map[string]string{
		"src/a.txt":   "needle one\nxx needle needle\n",
		"src/b.txt":   "héllo wörld needle\n",
		"src/sub/c":   "no match here\n",
		"src/bin.dat": "needle\xff\xfe",
	}
	for name, content := range files {
		h.do(http.MethodPut, "/files/content?path="+name, []byte(content), nil)
	}
	var found apitypes.FileMatches
	h.do(http.MethodPost, "/files/find", apitypes.FindInFilesRequest{Path: "src", Pattern: "needle"}, &found)
	a, b := filepath.Join(h.workspace, "src/a.txt"), filepath.Join(h.workspace, "src/b.txt")
	want := []apitypes.FileMatch{
		{Path: a, Line: 1, Column: 1, Text: "needle"},
		{Path: a, Line: 2, Column: 4, Text: "needle"},
		{Path: a, Line: 2, Column: 11, Text: "needle"},
		{Path: b, Line: 1, Column: 13, Text: "needle"},
	}
	if found.Truncated || len(found.Matches) != len(want) {
		t.Fatalf("matches %+v", found)
	}
	for i := range want {
		if found.Matches[i] != want[i] {
			t.Fatalf("match %d: %+v, want %+v", i, found.Matches[i], want[i])
		}
	}
	var replaced apitypes.ReplacedFiles
	h.do(http.MethodPost, "/files/replace", apitypes.ReplaceInFilesRequest{Path: "src", Pattern: "needle", Replacement: "pin"}, &replaced)
	if replaced.Files != 2 || replaced.Replacements != 4 {
		t.Fatalf("replaced %+v", replaced)
	}
	if data, _ := os.ReadFile(a); string(data) != "pin one\nxx pin pin\n" {
		t.Fatalf("rewritten %q", data)
	}
	if data, _ := os.ReadFile(filepath.Join(h.workspace, "src/bin.dat")); string(data) != files["src/bin.dat"] {
		t.Fatalf("binary file changed: %q", data)
	}
	h.expectError(http.MethodPost, "/files/find", apitypes.FindInFilesRequest{Path: "missing", Pattern: "x"}, http.StatusNotFound, apitypes.NotFound)
	h.expectError(http.MethodPost, "/files/find", apitypes.FindInFilesRequest{Path: "src", Pattern: ""}, http.StatusBadRequest, apitypes.InvalidRequest)

	h.do(http.MethodPut, "/files/content?path=many/m.txt", []byte(strings.Repeat("x\n", maxFindMatches+5)), nil)
	h.do(http.MethodPost, "/files/find", apitypes.FindInFilesRequest{Path: "many", Pattern: "x"}, &found)
	if len(found.Matches) != maxFindMatches || !found.Truncated || found.Matches[maxFindMatches-1].Line != maxFindMatches {
		t.Fatalf("capped find: %d matches, truncated %v", len(found.Matches), found.Truncated)
	}
}
