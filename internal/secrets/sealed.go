package secrets

import (
	"context"
	"encoding/json"
	"fmt"
)

// Seal encrypts value for another owner to store, bound to binding (such as
// "ssh-authority:<workspace id>"): the blob opens only with the same binding.
func (s *Secrets) Seal(_ context.Context, binding string, value []byte) ([]byte, error) {
	e, err := s.keys.seal(value, sealedData(binding))
	if err != nil {
		return nil, err
	}
	out, err := json.Marshal(e)
	if err != nil {
		return nil, fmt.Errorf("encode sealed value: %w", err)
	}
	return out, nil
}

// Open decrypts a blob Seal made with the same binding.
func (s *Secrets) Open(_ context.Context, binding string, blob []byte) ([]byte, error) {
	var e envelope
	if err := json.Unmarshal(blob, &e); err != nil {
		return nil, fmt.Errorf("decode sealed value: %w", err)
	}
	return s.keys.open(e, sealedData(binding))
}

func sealedData(binding string) []byte {
	return []byte("lazycloud-sealed:v1\x00" + binding)
}
