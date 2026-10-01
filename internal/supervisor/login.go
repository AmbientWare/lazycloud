package supervisor

import (
	"bufio"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

// workspaceDir is where relative paths of the control API resolve, and
// where processes and shells start.
const workspaceDir = "/workspace"

const defaultPath = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

// login is the account a shell or SSH session runs as. Sessions run with the
// supervisor's own identity; the account supplies the home and shell.
type login struct {
	user  string
	home  string
	shell string
}

// currentLogin is the passwd account of the supervisor's uid. It is read for
// every session, so a changed login shell takes effect at once.
func currentLogin() login {
	uid := strconv.Itoa(os.Getuid())
	l := login{user: uid, home: "/"}
	if uid == "0" {
		l = login{user: "root", home: "/root"}
	}
	if passwd, err := os.Open("/etc/passwd"); err == nil {
		scanner := bufio.NewScanner(passwd)
		for scanner.Scan() {
			fields := strings.Split(scanner.Text(), ":")
			if len(fields) < 7 || fields[2] != uid {
				continue
			}
			l.user = fields[0]
			if fields[5] != "" {
				l.home = fields[5]
			}
			if executableFile(fields[6]) {
				l.shell = fields[6]
			}
			break
		}
		_ = passwd.Close()
	}
	if l.shell == "" {
		l.shell = "/bin/sh"
		if executableFile("/bin/bash") {
			l.shell = "/bin/bash"
		}
	}
	return l
}

func executableFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir() && info.Mode()&0o111 != 0
}

// usableDir reports whether dir is a directory the supervisor can enter.
func usableDir(dir string) bool {
	info, err := os.Stat(dir)
	return err == nil && info.IsDir() && unix.Access(dir, unix.X_OK) == nil
}

// sessionDir is where a shell starts: the workspace when the container has
// one, else the login's home, else /.
func sessionDir(l login) string {
	for _, dir := range []string{workspaceDir, l.home} {
		if usableDir(dir) {
			return dir
		}
	}
	return "/"
}

// containerEnvironment is the container's environment without the agent
// link socket, which only the supervisor uses.
func containerEnvironment() []string {
	env := make([]string, 0, len(os.Environ()))
	for _, kv := range os.Environ() {
		if !strings.HasPrefix(kv, SocketEnv+"=") {
			env = append(env, kv)
		}
	}
	return env
}

// mergeEnvironment returns base with each NAME=value of overrides replacing
// or appending to it, in order.
func mergeEnvironment(base []string, overrides ...string) []string {
	index := make(map[string]int, len(base)+len(overrides))
	env := make([]string, 0, len(base)+len(overrides))
	for _, kv := range append(append([]string(nil), base...), overrides...) {
		name, _, ok := strings.Cut(kv, "=")
		if !ok {
			continue
		}
		if i, seen := index[name]; seen {
			env[i] = kv
			continue
		}
		index[name] = len(env)
		env = append(env, kv)
	}
	return env
}

// loginEnvironment is the container environment for a session of l.
func loginEnvironment(base []string, l login, extra ...string) []string {
	env := mergeEnvironment(base, append([]string{
		"HOME=" + l.home, "USER=" + l.user, "LOGNAME=" + l.user, "SHELL=" + l.shell,
	}, extra...)...)
	for _, kv := range env {
		if strings.HasPrefix(kv, "PATH=") && kv != "PATH=" {
			return env
		}
	}
	return mergeEnvironment(env, "PATH="+defaultPath)
}

// loginArgv0 is the argv[0] that makes a shell a login shell, such as -bash.
func loginArgv0(shell string) string {
	return "-" + filepath.Base(shell)
}
