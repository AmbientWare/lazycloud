package agent

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

type tlsHostService struct {
	hostproto.UnimplementedHostServiceServer
}

func (tlsHostService) Enroll(context.Context, *hostproto.EnrollRequest) (*hostproto.EnrollResponse, error) {
	return &hostproto.EnrollResponse{HostId: "h", HostToken: "t"}, nil
}

// writeCertificate writes a CA and a server certificate for 127.0.0.1 it
// signs, as the server's TLS listener loads them, and returns the CA, cert
// and key paths.
func writeCertificate(t *testing.T) (string, string, string) {
	t.Helper()
	dir := t.TempDir()
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	ca := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "test CA"}, IsCA: true, BasicConstraintsValid: true,
		KeyUsage: x509.KeyUsageCertSign, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
	}
	caDER, err := x509.CreateCertificate(rand.Reader, ca, ca, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	leaf := &x509.Certificate{
		SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: "server"}, IPAddresses: []net.IP{net.IPv4(127, 0, 0, 1)},
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, KeyUsage: x509.KeyUsageDigitalSignature,
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
	}
	caCert, err := x509.ParseCertificate(caDER)
	if err != nil {
		t.Fatal(err)
	}
	leafDER, err := x509.CreateCertificate(rand.Reader, leaf, caCert, &key.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	write := func(name, kind string, der []byte) string {
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: kind, Bytes: der}), 0o600); err != nil {
			t.Fatal(err)
		}
		return path
	}
	return write("ca.pem", "CERTIFICATE", caDER), write("cert.pem", "CERTIFICATE", leafDER), write("key.pem", "EC PRIVATE KEY", keyDER)
}

// The agent reaches a TLS listener only when it trusts the server's
// certificate, and refuses plaintext to anything but loopback.
func TestAgentDialsTheServerWithVerifiedTLS(t *testing.T) {
	caFile, certFile, keyFile := writeCertificate(t)
	creds, err := credentials.NewServerTLSFromFile(certFile, keyFile)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(creds))
	hostproto.RegisterHostServiceServer(server, tlsHostService{})
	lis, err := (&net.ListenConfig{}).Listen(t.Context(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	wg.Go(func() { _ = server.Serve(lis) })
	t.Cleanup(func() { server.Stop(); wg.Wait() })
	address := lis.Addr().String()

	enroll := func(cfg Config) error {
		conn, err := dialServer(cfg, "")
		if err != nil {
			return err
		}
		defer func() { _ = conn.Close() }()
		ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
		defer cancel()
		_, err = hostproto.NewHostServiceClient(conn).Enroll(ctx, &hostproto.EnrollRequest{})
		return err
	}
	if err := enroll(Config{Server: address, ServerCA: caFile}); err != nil {
		t.Fatalf("TLS with the server's CA: %v", err)
	}
	if err := enroll(Config{Server: address}); err == nil {
		t.Fatal("TLS without trusting the server's CA succeeded")
	}
	if err := enroll(Config{Server: address, ServerPlaintext: true}); err == nil {
		t.Fatal("plaintext to a TLS listener succeeded")
	}
	if err := enroll(Config{Server: "10.0.0.1:443", ServerPlaintext: true}); !errors.Is(err, ErrPlaintextRemote) {
		t.Fatalf("plaintext to a remote server: got %v, want ErrPlaintextRemote", err)
	}
}
