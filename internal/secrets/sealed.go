package secrets

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
)

// sealedBlob is a value another owner stores itself: a data key wrapped by
// the master key, and the value sealed under it.
type sealedBlob struct {
	KeyID      string `json:"key_id"`
	WrappedKey []byte `json:"wrapped_key"`
	Box        []byte `json:"box"`
}

// Seal encrypts value under a fresh data key for another owner to store,
// bound to binding (such as "ssh-authority:<workspace id>"): the blob opens
// only with the same binding.
func (s *Secrets) Seal(ctx context.Context, binding string, value []byte) ([]byte, error) {
	dataKey := make([]byte, masterKeyBytes)
	if _, err := rand.Read(dataKey); err != nil {
		return nil, fmt.Errorf("generate data key: %w", err)
	}
	aead, err := newAEAD(dataKey)
	if err != nil {
		return nil, err
	}
	box, err := seal(aead, value, sealedData(binding))
	if err != nil {
		return nil, err
	}
	wrapped, err := s.keys.Wrap(ctx, dataKey)
	if err != nil {
		return nil, fmt.Errorf("wrap data key: %w", err)
	}
	out, err := json.Marshal(sealedBlob{KeyID: s.keys.KeyID(), WrappedKey: wrapped, Box: box})
	if err != nil {
		return nil, fmt.Errorf("encode sealed value: %w", err)
	}
	return out, nil
}

// Open decrypts a blob Seal made with the same binding.
func (s *Secrets) Open(ctx context.Context, binding string, blob []byte) ([]byte, error) {
	var b sealedBlob
	if err := json.Unmarshal(blob, &b); err != nil {
		return nil, fmt.Errorf("decode sealed value: %w", err)
	}
	dataKey, err := s.keys.Unwrap(ctx, b.KeyID, b.WrappedKey)
	if err != nil {
		return nil, fmt.Errorf("unwrap data key: %w", err)
	}
	aead, err := newAEAD(dataKey)
	if err != nil {
		return nil, err
	}
	value, err := open(aead, b.Box, sealedData(binding))
	if err != nil {
		return nil, err
	}
	return value, nil
}

func sealedData(binding string) []byte {
	return []byte("lazycloud-sealed:v1\x00" + binding)
}
