package supervisor

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/pkg/sftp"
	"golang.org/x/crypto/ssh"

	"github.com/AmbientWare/lazycloud/internal/apitypes" //nolint:depguard // the control API bodies are the public schemas
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// tunnelConn reads through the buffer that held the upgrade response.
type tunnelConn struct {
	*net.UnixConn
	reader *bufio.Reader
}

func (c *tunnelConn) Read(p []byte) (int, error) { return c.reader.Read(p) }

// dialTunnel upgrades GET target to a tunnel, or returns the refusal's
// status and body.
func (h *controlHarness) dialTunnel(target string) (*tunnelConn, int, []byte) {
	h.t.Helper()
	raw, err := (&net.Dialer{}).DialContext(h.t.Context(), "unix", h.socket)
	if err != nil {
		h.t.Fatal(err)
	}
	conn := raw.(*net.UnixConn)
	h.t.Cleanup(func() { _ = conn.Close() })
	_, _ = fmt.Fprintf(conn, "GET %s HTTP/1.1\r\nHost: control\r\nConnection: Upgrade\r\nUpgrade: %s\r\n\r\n", target, TunnelProtocol)
	reader := bufio.NewReader(conn)
	resp, err := http.ReadResponse(reader, nil)
	if err != nil {
		h.t.Fatal(err)
	}
	if resp.StatusCode != http.StatusSwitchingProtocols {
		defer func() { _ = resp.Body.Close() }()
		body, _ := io.ReadAll(resp.Body)
		return nil, resp.StatusCode, body
	}
	return &tunnelConn{UnixConn: conn, reader: reader}, resp.StatusCode, nil
}

func TestPortTunnelSplicesBothWaysWithHalfClose(t *testing.T) {
	h := startControl(t, nil)
	listener, err := (&net.ListenConfig{}).Listen(t.Context(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = listener.Close() }()
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		// Answer only after the client's half-close arrives.
		data, _ := io.ReadAll(conn)
		_, _ = fmt.Fprintf(conn, "got %d bytes", len(data))
	}()
	port := listener.Addr().(*net.TCPAddr).Port
	conn, status, _ := h.dialTunnel("/ports/" + strconv.Itoa(port))
	if conn == nil {
		t.Fatalf("tunnel refused: %d", status)
	}
	if _, err := conn.Write(bytes.Repeat([]byte("x"), 100000)); err != nil {
		t.Fatal(err)
	}
	if err := conn.CloseWrite(); err != nil {
		t.Fatal(err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	answer, err := io.ReadAll(conn)
	if err != nil || string(answer) != "got 100000 bytes" {
		t.Fatalf("answer %q: %v", answer, err)
	}

	_ = listener.Close()
	_, status, body := h.dialTunnel("/ports/" + strconv.Itoa(port))
	var e apitypes.Error
	if err := json.Unmarshal(body, &e); err != nil || status != http.StatusBadGateway || e.Code != apitypes.Unavailable {
		t.Fatalf("refused port: %d %s %v", status, body, err)
	}
	h.expectError(http.MethodGet, "/ports/80", nil, http.StatusBadRequest, apitypes.InvalidRequest)
	h.expectError(http.MethodGet, "/ports/70000", nil, http.StatusBadRequest, apitypes.InvalidRequest)
}

// sshKeys is an SSH identity and a workspace authority signed into a test.
type sshKeys struct {
	identity *hostproto.SshServer
	ca       ssh.Signer
	hostKey  ssh.PublicKey
}

func newSSHKeys(t *testing.T) sshKeys {
	t.Helper()
	_, hostPriv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	block, err := ssh.MarshalPrivateKey(hostPriv, "")
	if err != nil {
		t.Fatal(err)
	}
	hostSigner, err := ssh.NewSignerFromKey(hostPriv)
	if err != nil {
		t.Fatal(err)
	}
	ca := newSigner(t)
	return sshKeys{
		identity: &hostproto.SshServer{HostKey: pem.EncodeToMemory(block), UserAuthority: string(ssh.MarshalAuthorizedKey(ca.PublicKey()))},
		ca:       ca, hostKey: hostSigner.PublicKey(),
	}
}

func newSigner(t *testing.T) ssh.Signer {
	t.Helper()
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := ssh.NewSignerFromKey(priv)
	if err != nil {
		t.Fatal(err)
	}
	return signer
}

// certificate signs a user certificate for principals with ca.
func certificate(t *testing.T, ca ssh.Signer, principals ...string) ssh.Signer {
	t.Helper()
	user := newSigner(t)
	cert := &ssh.Certificate{
		Key: user.PublicKey(), CertType: ssh.UserCert, KeyId: "test", ValidPrincipals: principals,
		ValidAfter: uint64(time.Now().Add(-time.Minute).Unix()), ValidBefore: uint64(time.Now().Add(time.Hour).Unix()), //nolint:gosec // positive times
	}
	if err := cert.SignCert(rand.Reader, ca); err != nil {
		t.Fatal(err)
	}
	signer, err := ssh.NewCertSigner(cert, user)
	if err != nil {
		t.Fatal(err)
	}
	return signer
}

func (h *controlHarness) sshClient(keys sshKeys, user string, signer ssh.Signer) (*ssh.Client, error) {
	h.t.Helper()
	conn, status, _ := h.dialTunnel("/ssh")
	if conn == nil {
		return nil, fmt.Errorf("ssh tunnel refused: %d", status)
	}
	config := &ssh.ClientConfig{
		User: user, Auth: []ssh.AuthMethod{ssh.PublicKeys(signer)},
		HostKeyCallback: ssh.FixedHostKey(keys.hostKey), Timeout: 10 * time.Second,
	}
	c, chans, reqs, err := ssh.NewClientConn(conn, "control", config)
	if err != nil {
		return nil, err //nolint:wrapcheck // the test reads the handshake error
	}
	client := ssh.NewClient(c, chans, reqs)
	h.t.Cleanup(func() { _ = client.Close() })
	return client, nil
}

func TestSSHAcceptsOnlyAuthorityCertificatesForRoot(t *testing.T) {
	keys := newSSHKeys(t)
	h := startControl(t, keys.identity)
	if _, err := h.sshClient(keys, "root", certificate(t, keys.ca, "root")); err != nil {
		t.Fatalf("valid certificate: %v", err)
	}
	refused := map[string]func() (*ssh.Client, error){
		"plain key":       func() (*ssh.Client, error) { return h.sshClient(keys, "root", newSigner(t)) },
		"other principal": func() (*ssh.Client, error) { return h.sshClient(keys, "root", certificate(t, keys.ca, "admin")) },
		"no principal":    func() (*ssh.Client, error) { return h.sshClient(keys, "root", certificate(t, keys.ca)) },
		"other user": func() (*ssh.Client, error) {
			return h.sshClient(keys, "ubuntu", certificate(t, keys.ca, "root", "ubuntu"))
		},
		"other authority": func() (*ssh.Client, error) { return h.sshClient(keys, "root", certificate(t, newSigner(t), "root")) },
	}
	for name, dial := range refused {
		if _, err := dial(); err == nil || !strings.Contains(err.Error(), "unable to authenticate") {
			t.Fatalf("%s: %v", name, err)
		}
	}
	h.ctl.ssh = nil
	h.expectError(http.MethodGet, "/ssh", nil, http.StatusNotFound, apitypes.NotFound)
}

func TestSSHSessionsExecShellSignalSFTPAndForwarding(t *testing.T) {
	keys := newSSHKeys(t)
	h := startControl(t, keys.identity)
	client, err := h.sshClient(keys, "root", certificate(t, keys.ca, "root"))
	if err != nil {
		t.Fatal(err)
	}

	session, err := client.NewSession()
	if err != nil {
		t.Fatal(err)
	}
	if err := session.Setenv("WR_SSH_VALUE", "sent"); err != nil {
		t.Fatal(err)
	}
	out, err := session.CombinedOutput(`echo "$WR_SSH_VALUE $USER"; exit 3`)
	var exitErr *ssh.ExitError
	if !errors.As(err, &exitErr) || exitErr.ExitStatus() != 3 || string(out) != "sent "+currentLogin().user+"\n" {
		t.Fatalf("exec: %q %v", out, err)
	}

	session, err = client.NewSession()
	if err != nil {
		t.Fatal(err)
	}
	if err := session.RequestPty("xterm-ssh", 30, 90, ssh.TerminalModes{}); err != nil {
		t.Fatal(err)
	}
	session.Stdin = strings.NewReader("echo \"$TERM $SSH_TTY\"; stty size; exit 4\n")
	var terminal bytes.Buffer
	session.Stdout = &terminal
	err = session.Shell()
	if err == nil {
		err = session.Wait()
	}
	if !errors.As(err, &exitErr) || exitErr.ExitStatus() != 4 || !strings.Contains(terminal.String(), "xterm-ssh /dev/pts/") ||
		!strings.Contains(terminal.String(), "30 90") {
		t.Fatalf("pty shell: %q %v", terminal.String(), err)
	}

	session, err = client.NewSession()
	if err != nil {
		t.Fatal(err)
	}
	if err := session.Start("sleep 100"); err != nil {
		t.Fatal(err)
	}
	time.Sleep(100 * time.Millisecond)
	if err := session.Signal(ssh.SIGTERM); err != nil {
		t.Fatal(err)
	}
	if err := session.Wait(); !errors.As(err, &exitErr) || exitErr.Signal() != "TERM" {
		t.Fatalf("signalled session: %v", err)
	}

	files, err := sftp.NewClient(client)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = files.Close() }()
	path := filepath.Join(h.workspace, "via-sftp.txt")
	f, err := files.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Write([]byte("over sftp")); err != nil {
		t.Fatal(err)
	}
	_ = f.Close()
	var got []byte
	h.do(http.MethodGet, "/files/content?path=via-sftp.txt", nil, &got)
	if string(got) != "over sftp" {
		t.Fatalf("sftp upload %q", got)
	}

	listener, err := (&net.ListenConfig{}).Listen(t.Context(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = listener.Close() }()
	go func() {
		conn, err := listener.Accept()
		if err == nil {
			_, _ = io.Copy(conn, conn)
			_ = conn.Close()
		}
	}()
	forwarded, err := client.DialContext(context.Background(), "tcp", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	_, _ = fmt.Fprint(forwarded, "echo me")
	buf := make([]byte, 7)
	if _, err := io.ReadFull(forwarded, buf); err != nil || string(buf) != "echo me" {
		t.Fatalf("forwarded %q %v", buf, err)
	}
	_ = forwarded.Close()
	if _, err := client.DialContext(context.Background(), "tcp", "192.0.2.1:80"); err == nil || !strings.Contains(err.Error(), "not permitted") {
		t.Fatalf("forwarding outside the container: %v", err)
	}
}
