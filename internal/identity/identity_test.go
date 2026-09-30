package identity

import (
	"errors"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

func TestWorkspaceAuthorization(t *testing.T) {
	ctx := t.Context()
	id := NewIdentity(dbtest.New(t))
	for _, u := range []struct {
		email string
		admin bool
	}{{"owner@example.com", false}, {"other@example.com", false}, {"admin@example.com", true}} {
		if _, err := id.CreateUser(ctx, u.email, u.admin); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := id.CreateWorkspace(ctx, "alpha", "owner@example.com"); err != nil {
		t.Fatal(err)
	}
	if _, err := id.CreateWorkspace(ctx, "beta", "owner@example.com"); err != nil {
		t.Fatal(err)
	}
	if _, err := id.CreateWorkspace(ctx, "alpha", "other@example.com"); !errors.Is(err, ErrExists) {
		t.Fatalf("duplicate workspace: got %v, want ErrExists", err)
	}

	principal := func(email, workspace string) Principal {
		t.Helper()
		token, err := id.CreateToken(ctx, email, workspace, "test")
		if err != nil {
			t.Fatal(err)
		}
		p, err := id.Authenticate(ctx, token)
		if err != nil {
			t.Fatal(err)
		}
		return p
	}
	owner := principal("owner@example.com", "")
	restricted := principal("owner@example.com", "alpha")
	other := principal("other@example.com", "")
	admin := principal("admin@example.com", "")
	adminRestricted := principal("admin@example.com", "alpha")

	cases := []struct {
		name      string
		p         Principal
		workspace string
		want      error
	}{
		{"member", owner, "alpha", nil},
		{"restricted token in its workspace", restricted, "alpha", nil},
		{"restricted token elsewhere", restricted, "beta", ErrForbidden},
		{"non-member", other, "alpha", ErrForbidden},
		{"non-member, missing workspace", other, "gamma", ErrForbidden},
		{"admin without membership", admin, "beta", nil},
		{"admin, missing workspace", admin, "gamma", ErrNotFound},
		{"restricted admin token elsewhere", adminRestricted, "beta", ErrForbidden},
	}
	for _, c := range cases {
		ws, err := id.AuthorizeWorkspace(ctx, c.p, c.workspace)
		if !errors.Is(err, c.want) {
			t.Errorf("%s: got %v, want %v", c.name, err, c.want)
			continue
		}
		if err == nil && ws.Name != c.workspace {
			t.Errorf("%s: got workspace %q", c.name, ws.Name)
		}
	}

	listed, err := id.Workspaces(ctx, restricted)
	if err != nil {
		t.Fatal(err)
	}
	if len(listed) != 1 || listed[0].Name != "alpha" {
		t.Fatalf("restricted token lists %v, want only alpha", listed)
	}

	if _, err := id.Authenticate(ctx, "lc_unknown"); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("unknown token: got %v, want ErrUnauthenticated", err)
	}
}
