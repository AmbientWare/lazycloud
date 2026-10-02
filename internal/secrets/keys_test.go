package secrets

import (
	"crypto/rand"
	"os"
	"path/filepath"
	"testing"
)

// A key file loads when only its owner, or also its group with this process
// in that group, can read it; any write or other access refuses it.
func TestKeyFilePermissions(t *testing.T) {
	key := make([]byte, masterKeyBytes)
	_, _ = rand.Read(key)
	for _, tc := range []struct {
		mode os.FileMode
		ok   bool
	}{
		{0o600, true}, {0o400, true}, {0o640, true}, {0o440, true},
		{0o660, false}, {0o604, false}, {0o644, false}, {0o650, false},
	} {
		path := filepath.Join(t.TempDir(), "secrets.key")
		if err := os.WriteFile(path, key, 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(path, tc.mode); err != nil {
			t.Fatal(err)
		}
		// The file's group is this process's group, as with an fsGroup mount.
		if _, err := LoadFileKey(path); (err == nil) != tc.ok {
			t.Errorf("mode %o: err %v, want ok %v", tc.mode, err, tc.ok)
		}
	}
	if memberOf(4242, 1000, []int{27, 1000}) {
		t.Error("a group the process is not in may read the key")
	}
	if !memberOf(65532, 0, []int{65532}) {
		t.Error("a supplementary group may not read the key")
	}
}
