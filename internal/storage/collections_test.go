package storage_test

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	. "github.com/AmbientWare/lazycloud/internal/storage"
)

func TestQueueDeliversEachMessageOnceInOrder(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	f := newFixture(t, `{}`)
	s := f.storage

	if _, found, err := s.PopQueueMessage(ctx, nil, f.ws, "jobs", 0); err != nil || found {
		t.Fatalf("pop of a queue never written: found=%v err=%v", found, err)
	}
	const total = 400
	for start := 0; start < total; start += 100 {
		batch := make([][]byte, 100)
		for n := range batch {
			batch[n] = fmt.Appendf(nil, "%d", start+n)
		}
		if err := s.PutQueueMessages(ctx, f.ws, "jobs", batch); err != nil {
			t.Fatal(err)
		}
		if info, _ := s.GetQueue(ctx, f.ws, "jobs"); info.Size != int64(start+100) {
			t.Fatalf("size after put: %d", info.Size)
		}
	}
	head, found, err := s.PeekQueueMessage(ctx, f.ws, "jobs")
	if err != nil || !found || string(head) != "0" {
		t.Fatalf("peek: %q found=%v err=%v", head, found, err)
	}

	// Concurrent pops never share a message and lose none.
	var mu sync.Mutex
	var got []int
	var wg sync.WaitGroup
	for range 8 {
		wg.Go(func() {
			var mine []int
			for {
				m, found, err := s.PopQueueMessage(ctx, nil, f.ws, "jobs", 0)
				if err != nil {
					t.Error(err)
					return
				}
				if !found {
					break
				}
				var v int
				_, _ = fmt.Sscan(string(m), &v)
				mine = append(mine, v)
			}
			if !slices.IsSorted(mine) {
				t.Errorf("one consumer saw messages out of order: %v", mine)
			}
			mu.Lock()
			got = append(got, mine...)
			mu.Unlock()
		})
	}
	wg.Wait()
	slices.Sort(got)
	if len(got) != total || got[0] != 0 || got[total-1] != total-1 || len(slices.Compact(got)) != total {
		t.Fatalf("popped %d messages, want each of %d once", len(got), total)
	}
	info, err := s.GetQueue(ctx, f.ws, "jobs")
	if err != nil || info.Size != 0 || info.OldestMessageAt != nil {
		t.Fatalf("drained queue: %+v err=%v", info, err)
	}
}

// emptyReads returns a pool on pool's database whose queries send on the
// returned channel when they come back empty, as a pop's read of an empty
// queue does just before it waits.
func emptyReads(t *testing.T, pool *pgxpool.Pool) (*pgxpool.Pool, <-chan struct{}) {
	t.Helper()
	cfg := pool.Config()
	empty := make(chan struct{}, 16)
	cfg.ConnConfig.Tracer = emptyTracer(empty)
	traced, err := pgxpool.NewWithConfig(t.Context(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(traced.Close)
	return traced, empty
}

type emptyTracer chan<- struct{}

func (emptyTracer) TraceQueryStart(ctx context.Context, _ *pgx.Conn, _ pgx.TraceQueryStartData) context.Context {
	return ctx
}

func (e emptyTracer) TraceQueryEnd(_ context.Context, _ *pgx.Conn, data pgx.TraceQueryEndData) {
	if data.Err == nil && data.CommandTag.RowsAffected() == 0 {
		select {
		case e <- struct{}{}:
		default:
		}
	}
}

func TestQueuePopWaitsForPut(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	f := newFixture(t, `{}`)
	listener := database.NewListener(f.pool, slog.Default(), ChannelQueue)
	listening, unsubscribe := listener.Subscribe(ChannelQueue, "")
	defer unsubscribe()
	go func() { _ = listener.Run(ctx) }()
	<-listening // Run wakes every subscriber once LISTEN is active.
	traced, empty := emptyReads(t, f.pool)
	waiter := NewStorage(traced, f.cfg, compute.NewCompute(traced, nil, compute.Config{}))

	type popped struct {
		m     []byte
		found bool
		err   error
	}
	done := make(chan popped, 1)
	pop := func() {
		m, found, err := waiter.PopQueueMessage(ctx, listener, f.ws, "wake", time.Minute)
		done <- popped{m, found, err}
	}
	// woken waits for the waiting pop to return; only the put's
	// notification ends its minute early.
	woken := func(want string) {
		t.Helper()
		select {
		case r := <-done:
			if r.err != nil || !r.found || string(r.m) != want {
				t.Fatalf("waiting pop: %q found=%v err=%v, want %s", r.m, r.found, r.err, want)
			}
		case <-time.After(10 * time.Second):
			t.Fatalf("the put of %s did not wake the waiting pop", want)
		}
	}

	go pop()
	<-empty // The pop found no queue and waits.
	if err := f.storage.PutQueueMessages(ctx, f.ws, "wake", [][]byte{[]byte(`"hello"`)}); err != nil {
		t.Fatal(err)
	}
	woken(`"hello"`)

	// A waiter keeps waiting by name across a delete and a new queue of
	// the same name.
	for len(empty) > 0 {
		<-empty
	}
	go pop()
	<-empty // The pop found the queue empty and waits.
	if err := f.storage.DeleteQueue(ctx, f.ws, "wake"); err != nil {
		t.Fatal(err)
	}
	if err := f.storage.PutQueueMessages(ctx, f.ws, "wake", [][]byte{[]byte(`"again"`)}); err != nil {
		t.Fatal(err)
	}
	woken(`"again"`)

	wait := 50 * time.Millisecond
	began := time.Now()
	_, found, err := f.storage.PopQueueMessage(ctx, listener, f.ws, "wake", wait)
	if err != nil || found || time.Since(began) < wait {
		t.Fatalf("pop of an empty queue returned found=%v err=%v after %v, want nothing after %v", found, err, time.Since(began), wait)
	}
}

func TestQueueDeleteAndSizeLimit(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	f := newFixture(t, `{}`)
	if err := f.storage.PutQueueMessages(ctx, f.ws, "q/with/slashes", [][]byte{[]byte("1"), {}}); err != nil {
		t.Fatal(err)
	}
	// A stored empty message is distinct from an empty queue.
	_, _, _ = f.storage.PopQueueMessage(ctx, nil, f.ws, "q/with/slashes", 0)
	m, found, err := f.storage.PopQueueMessage(ctx, nil, f.ws, "q/with/slashes", 0)
	if err != nil || !found || len(m) != 0 {
		t.Fatalf("empty message: %q found=%v err=%v", m, found, err)
	}
	if err := f.storage.PutQueueMessages(ctx, f.ws, "big", [][]byte{make([]byte, MaxValueBytes+1)}); !errors.Is(err, ErrTooLarge) {
		t.Fatalf("oversized message: %v", err)
	}
	if err := f.storage.DeleteQueue(ctx, f.ws, "q/with/slashes"); err != nil {
		t.Fatal(err)
	}
	page, err := f.storage.ListQueues(ctx, f.ws, "", 100)
	if err != nil || len(page.Queues) != 0 {
		t.Fatalf("queues after delete: %+v err=%v", page, err)
	}
}

func TestMapCompareAndSet(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	f := newFixture(t, `{}`)
	s := f.storage

	if _, err := s.GetMapEntry(ctx, f.ws, "m", "k"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing key: %v", err)
	}
	first, err := s.SetMapEntry(ctx, f.ws, "m", "k", SetMapEntry{Value: []byte(`1`)})
	if err != nil {
		t.Fatal(err)
	}
	second, err := s.SetMapEntry(ctx, f.ws, "m", "k", SetMapEntry{Value: []byte(`2`), IfRevision: &first.Revision})
	if err != nil || second.Revision == first.Revision {
		t.Fatalf("write at the current revision: %+v err=%v", second, err)
	}
	var stale *ConflictError
	if _, err := s.SetMapEntry(ctx, f.ws, "m", "k", SetMapEntry{Value: []byte(`3`), IfRevision: &first.Revision}); !errors.As(err, &stale) {
		t.Fatalf("write at a stale revision: %v", err)
	}
	if err := s.DeleteMapEntry(ctx, f.ws, "m", "k", &first.Revision); !errors.As(err, &stale) {
		t.Fatalf("delete at a stale revision: %v", err)
	}
	entry, err := s.GetMapEntry(ctx, f.ws, "m", "k")
	if err != nil || string(entry.Value) != `2` || entry.Revision != second.Revision {
		t.Fatalf("entry: %+v err=%v", entry, err)
	}

	// Of concurrent if_absent writes exactly one wins.
	var wins, conflicts int
	var mu sync.Mutex
	var wg sync.WaitGroup
	for n := range 16 {
		wg.Go(func() {
			_, err := s.SetMapEntry(ctx, f.ws, "m", "once", SetMapEntry{Value: fmt.Appendf(nil, "%d", n), IfAbsent: true})
			mu.Lock()
			defer mu.Unlock()
			var c *ConflictError
			switch {
			case err == nil:
				wins++
			case errors.As(err, &c):
				conflicts++
			default:
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if wins != 1 || conflicts != 15 {
		t.Fatalf("if_absent: %d wins, %d conflicts", wins, conflicts)
	}

	if err := s.DeleteMapEntry(ctx, f.ws, "m", "k", &second.Revision); err != nil {
		t.Fatal(err)
	}
	if err := s.DeleteMapEntry(ctx, f.ws, "m", "k", nil); !errors.Is(err, ErrNotFound) {
		t.Fatalf("delete of a deleted key: %v", err)
	}
	// A key written again after a delete never repeats a revision.
	again, err := s.SetMapEntry(ctx, f.ws, "m", "k", SetMapEntry{Value: []byte(`2`)})
	if err != nil || again.Revision == second.Revision || again.Revision == first.Revision {
		t.Fatalf("rewritten key revision %q, earlier %q and %q", again.Revision, first.Revision, second.Revision)
	}
}

func TestMapExpiryKeysAndStats(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	f := newFixture(t, `{}`)
	s := f.storage

	short := time.Hour
	never := time.Duration(0)
	for _, key := range []string{"b/2", "a/1", "a/3", "a/2", "c"} {
		if _, err := s.SetMapEntry(ctx, f.ws, "m", key, SetMapEntry{Value: []byte(`"v"`), TTL: &never}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := s.SetMapEntry(ctx, f.ws, "m", "a/expiring", SetMapEntry{Value: []byte(`"x"`), TTL: &short}); err != nil {
		t.Fatal(err)
	}
	day := 24 * time.Hour
	written, err := s.SetMapEntry(ctx, f.ws, "m", "c", SetMapEntry{Value: []byte(`"v2"`), TTL: &day})
	if err != nil || written.ExpiresAt == nil {
		t.Fatalf("ttl write: %+v err=%v", written, err)
	}
	// Without a ttl an existing key keeps its expiry.
	kept, err := s.SetMapEntry(ctx, f.ws, "m", "c", SetMapEntry{Value: []byte(`"v3"`)})
	if err != nil || kept.ExpiresAt == nil || !kept.ExpiresAt.Equal(*written.ExpiresAt) {
		t.Fatalf("write without ttl: %+v, want expiry %v (err %v)", kept, written.ExpiresAt, err)
	}
	tooLong := 8 * day
	var bad *InvalidError
	if _, err := s.SetMapEntry(ctx, f.ws, "m", "c", SetMapEntry{Value: []byte(`1`), TTL: &tooLong}); !errors.As(err, &bad) {
		t.Fatalf("ttl above 7 days: %v", err)
	}

	page, err := s.ListMapKeys(ctx, f.ws, "m", "a/", "", 2)
	if err != nil || !slices.Equal(page.Keys, []string{"a/1", "a/2"}) || page.NextCursor == nil {
		t.Fatalf("first prefix page: %+v err=%v", page, err)
	}
	page, err = s.ListMapKeys(ctx, f.ws, "m", "a/", *page.NextCursor, 2)
	if err != nil || !slices.Equal(page.Keys, []string{"a/3", "a/expiring"}) {
		t.Fatalf("second prefix page: %+v err=%v", page, err)
	}

	if _, err := f.pool.Exec(ctx, `update map_entries set expires_at = now() - interval '1 second' where key = 'a/expiring'`); err != nil {
		t.Fatal(err)
	}
	if _, err := s.GetMapEntry(ctx, f.ws, "m", "a/expiring"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("expired key: %v", err)
	}
	info, err := s.GetMap(ctx, f.ws, "m")
	if err != nil || info.Count != 5 || info.ExpiringCount != 1 || info.NextExpiryAt == nil {
		t.Fatalf("stats: %+v err=%v", info, err)
	}
	if _, err := s.Sweep(ctx, slog.Default()); err != nil {
		t.Fatal(err)
	}
	var rows int
	if err := f.pool.QueryRow(ctx, `select count(*) from map_entries`).Scan(&rows); err != nil || rows != 5 {
		t.Fatalf("rows after sweep: %d err=%v", rows, err)
	}
	maps, err := s.ListMaps(ctx, f.ws, "", 100)
	if err != nil || len(maps.Maps) != 1 || maps.Maps[0].Count != 5 {
		t.Fatalf("maps: %+v err=%v", maps, err)
	}
	if err := s.DeleteMap(ctx, f.ws, "m"); err != nil {
		t.Fatal(err)
	}
	if info, err := s.GetMap(ctx, f.ws, "m"); err != nil || info.Count != 0 {
		t.Fatalf("deleted map: %+v err=%v", info, err)
	}
}
