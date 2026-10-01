package supervisor

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"golang.org/x/sys/unix"
)

type shellClient struct {
	t    *testing.T
	conn *websocket.Conn
	// output is every terminal byte received so far.
	output strings.Builder
}

func (h *controlHarness) openShell(query string) *shellClient {
	h.t.Helper()
	ctx, cancel := context.WithTimeout(h.t.Context(), 10*time.Second)
	defer cancel()
	conn, _, err := websocket.Dial(ctx, "ws://control/shell"+query, &websocket.DialOptions{HTTPClient: h.client}) //nolint:bodyclose // the WebSocket owns the body
	if err != nil {
		h.t.Fatal(err)
	}
	h.t.Cleanup(func() { _ = conn.CloseNow() })
	return &shellClient{t: h.t, conn: conn}
}

func (s *shellClient) send(text string) {
	s.t.Helper()
	if err := s.conn.Write(s.t.Context(), websocket.MessageBinary, []byte(text)); err != nil {
		s.t.Fatal(err)
	}
}

// until reads until the terminal output contains want, returning the text
// messages seen on the way.
func (s *shellClient) until(want string) []shellMessage {
	s.t.Helper()
	ctx, cancel := context.WithTimeout(s.t.Context(), 10*time.Second)
	defer cancel()
	var messages []shellMessage
	for !strings.Contains(s.output.String(), want) {
		kind, data, err := s.conn.Read(ctx)
		if err != nil {
			s.t.Fatalf("waiting for %q in %q: %v", want, s.output.String(), err)
		}
		if kind == websocket.MessageBinary {
			s.output.Write(data)
			continue
		}
		var m shellMessage
		if err := json.Unmarshal(data, &m); err != nil {
			s.t.Fatal(err)
		}
		messages = append(messages, m)
		if m.Type == "exit" && want == "" {
			return messages
		}
	}
	return messages
}

// exit reads to the exit message and the normal close after it.
func (s *shellClient) exit() int {
	s.t.Helper()
	ctx, cancel := context.WithTimeout(s.t.Context(), 10*time.Second)
	defer cancel()
	code := -1
	for {
		kind, data, err := s.conn.Read(ctx)
		if err != nil {
			if websocket.CloseStatus(err) != websocket.StatusNormalClosure || code < 0 {
				s.t.Fatalf("close %v after exit code %d", err, code)
			}
			return code
		}
		if kind == websocket.MessageBinary {
			s.output.Write(data)
			continue
		}
		var m shellMessage
		if json.Unmarshal(data, &m) == nil && m.Type == "exit" && m.Code != nil {
			code = *m.Code
		}
	}
}

func TestShellRunsALoginShellOnAPTY(t *testing.T) {
	h := startControl(t, nil)
	s := h.openShell("?cols=100&rows=40&term=xterm-test")
	s.send("echo \"T=$TERM H=$HOME P=$(pwd) A=$0\"; stty size; tty; echo DO\"\"NE\n")
	s.until("DONE")
	out := s.output.String()
	for _, want := range []string{"T=xterm-test", "H=" + currentLogin().home, "P=" + workspaceDir, "40 100", "/dev/pts/"} {
		if want == "P="+workspaceDir && !usableDir(workspaceDir) {
			want = "P=" + sessionDir(currentLogin())
		}
		if !strings.Contains(out, want) {
			t.Fatalf("shell output %q lacks %q", out, want)
		}
	}
	if !strings.Contains(out, "A=-") {
		t.Fatalf("not a login shell: %q", out)
	}
	if err := s.conn.Write(t.Context(), websocket.MessageText, []byte(`{"type":"resize","cols":50,"rows":12}`)); err != nil {
		t.Fatal(err)
	}
	s.send("stty size\n")
	s.until("12 50")
	s.send("exit 7\n")
	if code := s.exit(); code != 7 {
		t.Fatalf("exit code %d", code)
	}
}

func TestShellDisconnectHangsUpTheSession(t *testing.T) {
	h := startControl(t, nil)
	s := h.openShell("")
	// The shell ignores SIGHUP, so it takes the SIGKILL after the grace.
	s.send("trap '' HUP; echo p\"\"id=$$ DO\"\"NE\n")
	s.until("DONE")
	text := s.output.String()
	start := strings.Index(text, "pid=") + len("pid=")
	end := start
	for end < len(text) && text[end] >= '0' && text[end] <= '9' {
		end++
	}
	pid, err := strconv.Atoi(text[start:end])
	if err != nil {
		t.Fatalf("pid in %q: %v", text, err)
	}
	_ = s.conn.CloseNow()
	closed := time.Now()
	for unix.Kill(pid, 0) == nil {
		if time.Since(closed) > 5*time.Second {
			t.Fatal("the shell outlived its client")
		}
		time.Sleep(20 * time.Millisecond)
	}
	if elapsed := time.Since(closed); elapsed < hangupGrace/2 {
		t.Fatalf("a shell ignoring SIGHUP ended after %s", elapsed)
	}
	t.Logf("disconnect to the end of a shell ignoring SIGHUP: %s", time.Since(closed))
}

func TestShellRejectsInvalidSizes(t *testing.T) {
	h := startControl(t, nil)
	_, resp, err := websocket.Dial(t.Context(), "ws://control/shell?cols=0", &websocket.DialOptions{HTTPClient: h.client})
	if err == nil || resp == nil || resp.StatusCode != 400 {
		t.Fatalf("dial with cols=0: %v", err)
	}
	_ = resp.Body.Close()
}
