package secrets

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"slices"
	"syscall"
)

// ErrUnknownKey means a secret was wrapped by a master key this server does
// not hold.
var ErrUnknownKey = errors.New("secret was wrapped by an unknown master key")

// masterKeyBytes is the length of a key file: an AES-256 key.
const masterKeyBytes = 32

// FileKey is the master key, read from a local file of 32 random bytes,
// that wraps every per-secret data key outside PostgreSQL.
type FileKey struct {
	id   string
	aead cipher.AEAD
	key  []byte
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
	return &FileKey{id: "file:" + hex.EncodeToString(sum[:8]), aead: aead, key: slices.Clone(key)}, nil
}

// Derive returns a 32-byte key for purpose, the same from every process
// that holds this master key and unrelated to it or to another purpose.
func (k *FileKey) Derive(purpose string) []byte {
	mac := hmac.New(sha256.New, k.key)
	mac.Write([]byte(purpose))
	return mac.Sum(nil)
}

// envelope is a value sealed under a fresh data key and the data key wrapped
// by the master key. KeyID names that master key by a digest prefix, so a
// replaced key file is recognized rather than producing authentication
// failures. Box and WrappedKey are nonce || ciphertext.
type envelope struct {
	KeyID      string `json:"key_id"`
	WrappedKey []byte `json:"wrapped_key"`
	Box        []byte `json:"box"`
}

// gcmNonceSize is the nonce length at the front of every box.
const gcmNonceSize = 12

// seal encrypts plaintext, bound to additional, under a fresh data key.
func (k *FileKey) seal(plaintext, additional []byte) (envelope, error) {
	dataKey := make([]byte, masterKeyBytes)
	if _, err := rand.Read(dataKey); err != nil {
		return envelope{}, fmt.Errorf("generate data key: %w", err)
	}
	aead, err := newAEAD(dataKey)
	if err != nil {
		return envelope{}, err
	}
	box, err := sealBox(aead, plaintext, additional)
	if err != nil {
		return envelope{}, err
	}
	wrapped, err := sealBox(k.aead, dataKey, []byte(k.id))
	if err != nil {
		return envelope{}, fmt.Errorf("wrap data key: %w", err)
	}
	return envelope{KeyID: k.id, WrappedKey: wrapped, Box: box}, nil
}

// open decrypts an envelope seal made with the same additional data.
func (k *FileKey) open(e envelope, additional []byte) ([]byte, error) {
	if e.KeyID != k.id {
		return nil, fmt.Errorf("unwrap data key: %w: %s", ErrUnknownKey, e.KeyID)
	}
	dataKey, err := openBox(k.aead, e.WrappedKey, []byte(k.id))
	if err != nil {
		return nil, fmt.Errorf("unwrap data key: %w", err)
	}
	aead, err := newAEAD(dataKey)
	if err != nil {
		return nil, err
	}
	return openBox(aead, e.Box, additional)
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

// sealBox returns nonce || ciphertext.
func sealBox(aead cipher.AEAD, plaintext, additional []byte) ([]byte, error) {
	nonce := make([]byte, aead.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return nil, fmt.Errorf("generate nonce: %w", err)
	}
	return aead.Seal(nonce, nonce, plaintext, additional), nil
}

func openBox(aead cipher.AEAD, sealed, additional []byte) ([]byte, error) {
	if len(sealed) < aead.NonceSize() {
		return nil, errors.New("sealed data is too short")
	}
	plain, err := aead.Open(nil, sealed[:aead.NonceSize()], sealed[aead.NonceSize():], additional)
	if err != nil {
		return nil, fmt.Errorf("open sealed data: %w", err)
	}
	return plain, nil
}
