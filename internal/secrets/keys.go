package secrets

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"slices"
	"syscall"
)

// KeyWrapper wraps per-secret data keys with a master key held outside
// PostgreSQL. A key file serves local development; production supplies a
// KMS implementation at the provider boundary.
type KeyWrapper interface {
	// KeyID names the master key Wrap uses. Stored with each secret, it
	// lets Unwrap find the key that wrapped an older value.
	KeyID() string
	Wrap(ctx context.Context, dataKey []byte) ([]byte, error)
	Unwrap(ctx context.Context, keyID string, wrapped []byte) ([]byte, error)
}

// ErrUnknownKey means a secret was wrapped by a master key this server does
// not hold.
var ErrUnknownKey = errors.New("secret was wrapped by an unknown master key")

// masterKeyBytes is the length of a key file: an AES-256 key.
const masterKeyBytes = 32

// FileKey is a master key read from a local file of 32 random bytes.
type FileKey struct {
	id   string
	aead cipher.AEAD
}

// LoadFileKey reads a master key file. The file must hold exactly 32 bytes
// and be readable only by its owner, or also by its group when this process
// is in that group: Kubernetes mounts secret files root-owned and readable
// by the pod's fsGroup.
func LoadFileKey(path string) (*FileKey, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, fmt.Errorf("secrets key file: %w", err)
	}
	if !privateKeyFile(info) {
		return nil, fmt.Errorf("secrets key file %s is accessible to other users; chmod 600 it", path)
	}
	key, err := os.ReadFile(path) //nolint:gosec // The operator names the key file.
	if err != nil {
		return nil, fmt.Errorf("read secrets key file: %w", err)
	}
	if len(key) != masterKeyBytes {
		return nil, fmt.Errorf("secrets key file %s holds %d bytes; it must hold %d random bytes", path, len(key), masterKeyBytes)
	}
	return NewFileKey(key)
}

// privateKeyFile reports whether only the owner, and the group when this
// process belongs to it, can read the file, and nobody else can write it.
func privateKeyFile(info os.FileInfo) bool {
	perm := info.Mode().Perm()
	if perm&0o077 == 0 {
		return true
	}
	if perm&0o037 != 0 {
		return false
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return false
	}
	groups, err := os.Getgroups()
	if err != nil {
		return false
	}
	return memberOf(int(stat.Gid), os.Getgid(), groups)
}

// memberOf reports whether gid is the process group or a supplementary one.
func memberOf(gid, own int, supplementary []int) bool {
	return gid == own || slices.Contains(supplementary, gid)
}

// NewFileKey uses key, 32 bytes, as the master key.
func NewFileKey(key []byte) (*FileKey, error) {
	aead, err := newAEAD(key)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(key)
	return &FileKey{id: "file:" + hex.EncodeToString(sum[:8]), aead: aead}, nil
}

// KeyID names the key by a digest prefix, so a replaced key file is
// recognized rather than producing authentication failures.
func (k *FileKey) KeyID() string { return k.id }

// Wrap seals dataKey under the master key.
func (k *FileKey) Wrap(_ context.Context, dataKey []byte) ([]byte, error) {
	return seal(k.aead, dataKey, []byte(k.id))
}

// Unwrap opens a data key that Wrap sealed.
func (k *FileKey) Unwrap(_ context.Context, keyID string, wrapped []byte) ([]byte, error) {
	if keyID != k.id {
		return nil, fmt.Errorf("%w: %s", ErrUnknownKey, keyID)
	}
	return open(k.aead, wrapped, []byte(k.id))
}

func newAEAD(key []byte) (cipher.AEAD, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("create cipher: %w", err)
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("create gcm: %w", err)
	}
	return aead, nil
}

// seal returns nonce || ciphertext.
func seal(aead cipher.AEAD, plaintext, additional []byte) ([]byte, error) {
	nonce := make([]byte, aead.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return nil, fmt.Errorf("generate nonce: %w", err)
	}
	return aead.Seal(nonce, nonce, plaintext, additional), nil
}

func open(aead cipher.AEAD, sealed, additional []byte) ([]byte, error) {
	if len(sealed) < aead.NonceSize() {
		return nil, errors.New("sealed data is too short")
	}
	plain, err := aead.Open(nil, sealed[:aead.NonceSize()], sealed[aead.NonceSize():], additional)
	if err != nil {
		return nil, fmt.Errorf("open sealed data: %w", err)
	}
	return plain, nil
}
