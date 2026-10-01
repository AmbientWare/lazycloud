package compute

import (
	"errors"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

func TestEnrollmentAndSessionEpochs(t *testing.T) {
	ctx := t.Context()
	pool := dbtest.New(t)
	c := NewCompute(pool)
	capacity := Capacity{CPUMillis: 4000, MemoryBytes: 8 << 30}

	join, _, err := c.CreateJoinToken(ctx, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	host, token, err := c.Enroll(ctx, join, "host-a", capacity)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.Enroll(ctx, join, "host-b", capacity); !errors.Is(err, ErrInvalidJoinToken) {
		t.Fatalf("reused join token: got %v, want ErrInvalidJoinToken", err)
	}
	expired, _, err := c.CreateJoinToken(ctx, -time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.Enroll(ctx, expired, "host-c", capacity); !errors.Is(err, ErrInvalidJoinToken) {
		t.Fatalf("expired join token: got %v, want ErrInvalidJoinToken", err)
	}

	if got, err := c.AuthenticateHost(ctx, token); err != nil || got != host {
		t.Fatalf("authenticate: got %v, %v", got, err)
	}
	if _, err := c.AuthenticateHost(ctx, join); !errors.Is(err, ErrUnknownHost) {
		t.Fatalf("join token as host token: got %v, want ErrUnknownHost", err)
	}

	first, err := c.OpenSession(ctx, host, "boot-1", capacity)
	if err != nil {
		t.Fatal(err)
	}
	if current, err := c.Touch(ctx, host, first); err != nil || !current {
		t.Fatalf("touch first session: current=%v err=%v", current, err)
	}
	second, err := c.OpenSession(ctx, host, "boot-1", capacity)
	if err != nil {
		t.Fatal(err)
	}
	if current, err := c.Touch(ctx, host, first); err != nil || current {
		t.Fatalf("superseded session: current=%v err=%v, want not current", current, err)
	}
	if current, err := c.Touch(ctx, host, second); err != nil || !current {
		t.Fatalf("latest session: current=%v err=%v", current, err)
	}
	if _, err := pool.Exec(ctx, "update hosts set state = 'lost' where id = $1", host.String()); err != nil {
		t.Fatal(err)
	}
	if current, err := c.Touch(ctx, host, second); err != nil || current {
		t.Fatalf("lost host: current=%v err=%v, want not current", current, err)
	}
}
