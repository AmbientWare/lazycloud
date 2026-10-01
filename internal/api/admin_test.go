package api_test

import (
	"testing"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

func TestAccountAdministrationOverHTTP(t *testing.T) {
	e := newEnv(t)
	ctx := t.Context()
	if _, err := e.identity.CreateUser(ctx, "admin@example.com", true); err != nil {
		t.Fatal(err)
	}
	admin, err := e.identity.CreateToken(ctx, "admin@example.com", "", "test")
	if err != nil {
		t.Fatal(err)
	}
	restricted, err := e.identity.CreateToken(ctx, "admin@example.com", "acme", "test")
	if err != nil {
		t.Fatal(err)
	}

	var apiErr apitypes.Error
	for name, token := range map[string]string{"member": e.owner, "restricted admin token": restricted} {
		if status := e.do("GET", "/v1/users", token, nil, &apiErr); status != 403 || apiErr.Code != apitypes.Forbidden {
			t.Fatalf("%s lists: %d %+v", name, status, apiErr)
		}
	}

	var list apitypes.UserList
	if status := e.do("GET", "/v1/users?role=member&status=active&search=OUTSIDER&limit=1", admin, nil, &list); status != 200 ||
		len(list.Users) != 1 || list.Users[0].Email != "outsider@example.com" || list.NextCursor != nil {
		t.Fatalf("filtered list: %d %+v", status, list)
	}
	outsider := list.Users[0].Id
	if status := e.do("GET", "/v1/users?limit=1", admin, nil, &list); status != 200 || len(list.Users) != 1 || list.NextCursor == nil {
		t.Fatalf("first page: %d %+v", status, list)
	}
	if status := e.do("GET", "/v1/users?limit=50&cursor="+list.NextCursor.String(), admin, nil, &list); status != 200 || len(list.Users) != 2 {
		t.Fatalf("second page: %d %+v", status, list)
	}
	if status := e.do("GET", "/v1/users?role=owner", admin, nil, &apiErr); status != 400 {
		t.Fatalf("unknown role: %d %+v", status, apiErr)
	}

	var user apitypes.User
	if status := e.do("PUT", "/v1/users/"+outsider.String()+"/role", admin, map[string]string{"role": "administrator"}, &user); status != 200 || !user.IsAdmin {
		t.Fatalf("promote: %d %+v", status, user)
	}
	if status := e.do("PUT", "/v1/users/"+outsider.String()+"/status", admin, map[string]string{"status": "disabled"}, &user); status != 200 || user.Status != apitypes.UserStatusDisabled {
		t.Fatalf("disable: %d %+v", status, user)
	}
	if status := e.do("GET", "/v1/me", e.outsider, nil, &apiErr); status != 401 {
		t.Fatalf("disabled account's token: %d", status)
	}

	var me apitypes.Me
	if status := e.do("GET", "/v1/me", admin, nil, &me); status != 200 || me.User.Status != apitypes.UserStatusActive {
		t.Fatalf("me: %d %+v", status, me)
	}
	if status := e.do("PUT", "/v1/users/"+me.User.Id.String()+"/status", admin, map[string]string{"status": "disabled"}, &apiErr); status != 409 || apiErr.Code != apitypes.Conflict {
		t.Fatalf("self disable: %d %+v", status, apiErr)
	}
}
