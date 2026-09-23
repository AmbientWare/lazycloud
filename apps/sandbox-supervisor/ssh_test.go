package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/pem"
	"errors"
	"io"
	"net"
	"os/exec"
	"strings"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"
)

type directProcesses struct{}

func (directProcesses) startChild(command *exec.Cmd) error { return command.Start() }

func (directProcesses) waitChild(command *exec.Cmd) error { return command.Wait() }

type sshTestServer struct {
	address   string
	hostKey   ssh.PublicKey
	authority ssh.Signer
}

func startSSHTestServer(t *testing.T) sshTestServer {
	t.Helper()
	_, hostPrivate, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	hostBlock, err := ssh.MarshalPrivateKey(hostPrivate, "")
	if err != nil {
		t.Fatal(err)
	}
	authority := newTestSigner(t)
	server, err := newSSHServer(
		directProcesses{},
		pem.EncodeToMemory(hostBlock),
		ssh.MarshalAuthorizedKey(authority.PublicKey()),
	)
	if err != nil {
		t.Fatal(err)
	}
	server.login = sshLogin{user: sshLoginUser, home: t.TempDir(), shell: "/bin/sh"}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	go server.serve(listener)
	hostSigner, err := ssh.NewSignerFromKey(hostPrivate)
	if err != nil {
		t.Fatal(err)
	}
	return sshTestServer{address: listener.Addr().String(), hostKey: hostSigner.PublicKey(), authority: authority}
}

func newTestSigner(t *testing.T) ssh.Signer {
	t.Helper()
	_, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := ssh.NewSignerFromKey(private)
	if err != nil {
		t.Fatal(err)
	}
	return signer
}

func certificateSigner(
	t *testing.T,
	authority ssh.Signer,
	principals []string,
	validAfter time.Time,
	validBefore time.Time,
) ssh.Signer {
	t.Helper()
	user := newTestSigner(t)
	certificate := &ssh.Certificate{
		Key:             user.PublicKey(),
		CertType:        ssh.UserCert,
		KeyId:           "test-user",
		ValidPrincipals: principals,
		ValidAfter:      uint64(validAfter.Unix()),
		ValidBefore:     uint64(validBefore.Unix()),
		Permissions:     ssh.Permissions{Extensions: map[string]string{"permit-pty": ""}},
	}
	if err := certificate.SignCert(rand.Reader, authority); err != nil {
		t.Fatal(err)
	}
	signer, err := ssh.NewCertSigner(certificate, user)
	if err != nil {
		t.Fatal(err)
	}
	return signer
}

func dialSSH(server sshTestServer, user string, signer ssh.Signer) (*ssh.Client, error) {
	return ssh.Dial("tcp", server.address, &ssh.ClientConfig{
		User:            user,
		Auth:            []ssh.AuthMethod{ssh.PublicKeys(signer)},
		HostKeyCallback: ssh.FixedHostKey(server.hostKey),
		Timeout:         5 * time.Second,
	})
}

func TestSSHRefusesCredentialsTheWorkspaceAuthorityDidNotIssue(t *testing.T) {
	server := startSSHTestServer(t)
	now := time.Now()
	cases := map[string]struct {
		user   string
		signer ssh.Signer
	}{
		"plain public key": {user: sshLoginUser, signer: newTestSigner(t)},
		"other authority": {
			user:   sshLoginUser,
			signer: certificateSigner(t, newTestSigner(t), []string{sshLoginUser}, now.Add(-time.Minute), now.Add(time.Hour)),
		},
		"other principal": {
			user:   sshLoginUser,
			signer: certificateSigner(t, server.authority, []string{"ubuntu"}, now.Add(-time.Minute), now.Add(time.Hour)),
		},
		"no principal": {
			user:   sshLoginUser,
			signer: certificateSigner(t, server.authority, nil, now.Add(-time.Minute), now.Add(time.Hour)),
		},
		"expired": {
			user:   sshLoginUser,
			signer: certificateSigner(t, server.authority, []string{sshLoginUser}, now.Add(-2*time.Hour), now.Add(-time.Hour)),
		},
		"other login user": {
			user:   "ubuntu",
			signer: certificateSigner(t, server.authority, []string{sshLoginUser, "ubuntu"}, now.Add(-time.Minute), now.Add(time.Hour)),
		},
	}
	for name, item := range cases {
		t.Run(name, func(t *testing.T) {
			client, err := dialSSH(server, item.user, item.signer)
			if err == nil {
				_ = client.Close()
				t.Fatal("server accepted a credential it must refuse")
			}
			if !strings.Contains(err.Error(), "unable to authenticate") {
				t.Fatalf("expected an authentication failure, got %v", err)
			}
		})
	}
}

func TestSSHSessionRunsCommandsAndForwardsOnlyLocalDestinations(t *testing.T) {
	server := startSSHTestServer(t)
	now := time.Now()
	client, err := dialSSH(server, sshLoginUser, certificateSigner(t, server.authority, []string{sshLoginUser}, now.Add(-time.Minute), now.Add(time.Hour)))
	if err != nil {
		t.Fatalf("certificate from the workspace authority was refused: %v", err)
	}
	defer client.Close()

	session, err := client.NewSession()
	if err != nil {
		t.Fatal(err)
	}
	if err := session.Setenv("GREETING", "hello"); err != nil {
		t.Fatal(err)
	}
	session.Stdin = strings.NewReader("from-stdin")
	var stdout, stderr bytes.Buffer
	session.Stdout, session.Stderr = &stdout, &stderr
	err = session.Run(`read line; echo "$GREETING $USER $line"; echo oops >&2; exit 3`)
	var exitErr *ssh.ExitError
	if !errors.As(err, &exitErr) || exitErr.ExitStatus() != 3 {
		t.Fatalf("expected exit status 3, got %v", err)
	}
	if got := stdout.String(); got != "hello root from-stdin\n" {
		t.Fatalf("unexpected stdout %q", got)
	}
	if got := stderr.String(); got != "oops\n" {
		t.Fatalf("unexpected stderr %q", got)
	}

	target, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer target.Close()
	go func() {
		connection, acceptErr := target.Accept()
		if acceptErr != nil {
			return
		}
		defer connection.Close()
		_, _ = connection.Write([]byte("forwarded"))
	}()
	forwarded, err := client.Dial("tcp", target.Addr().String())
	if err != nil {
		t.Fatalf("loopback forwarding was refused: %v", err)
	}
	received, err := io.ReadAll(forwarded)
	_ = forwarded.Close()
	if err != nil || string(received) != "forwarded" {
		t.Fatalf("forwarded stream returned %q, %v", received, err)
	}
	if connection, err := client.Dial("tcp", "192.0.2.1:80"); err == nil {
		_ = connection.Close()
		t.Fatal("forwarding to a destination outside the container was allowed")
	} else if !strings.Contains(err.Error(), "not permitted") {
		t.Fatalf("expected a prohibited forward, got %v", err)
	}
}
