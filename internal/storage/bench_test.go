package storage_test

import (
	"fmt"
	"sync/atomic"
	"testing"

	. "github.com/AmbientWare/lazycloud/internal/storage"
)

// Run with -bench . -benchtime 5000x.

func BenchmarkQueuePut(b *testing.B) {
	f := newFixture(b, `{}`)
	ctx := b.Context()
	message := [][]byte{[]byte(`{"job": 1}`)}
	b.ResetTimer()
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			if err := f.storage.PutQueueMessages(ctx, f.ws, "bench", message); err != nil {
				b.Error(err)
				return
			}
		}
	})
}

func BenchmarkQueuePop(b *testing.B) {
	f := newFixture(b, `{}`)
	ctx := b.Context()
	batch := make([][]byte, 1000)
	for n := range batch {
		batch[n] = fmt.Appendf(nil, `{"job": %d}`, n)
	}
	for put := 0; put < b.N+1000; put += len(batch) {
		if err := f.storage.PutQueueMessages(ctx, f.ws, "bench", batch); err != nil {
			b.Fatal(err)
		}
	}
	var empty atomic.Int64
	b.ResetTimer()
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			_, found, err := f.storage.PopQueueMessage(ctx, nil, f.ws, "bench", 0)
			if err != nil {
				b.Error(err)
				return
			}
			if !found {
				empty.Add(1)
			}
		}
	})
	if empty.Load() > 0 {
		b.Fatalf("%d pops found the queue empty", empty.Load())
	}
}

func BenchmarkMapSet(b *testing.B) {
	f := newFixture(b, `{}`)
	ctx := b.Context()
	var n atomic.Int64
	b.ResetTimer()
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			key := fmt.Sprintf("key-%d", n.Add(1)%1000)
			if _, err := f.storage.SetMapEntry(ctx, f.ws, "bench", key, SetMapEntry{Value: []byte(`"value"`)}); err != nil {
				b.Error(err)
				return
			}
		}
	})
}

func BenchmarkMapGet(b *testing.B) {
	f := newFixture(b, `{}`)
	ctx := b.Context()
	for n := range 1000 {
		if _, err := f.storage.SetMapEntry(ctx, f.ws, "bench", fmt.Sprintf("key-%d", n), SetMapEntry{Value: []byte(`"value"`)}); err != nil {
			b.Fatal(err)
		}
	}
	var n atomic.Int64
	b.ResetTimer()
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			if _, err := f.storage.GetMapEntry(ctx, f.ws, "bench", fmt.Sprintf("key-%d", n.Add(1)%1000)); err != nil {
				b.Error(err)
				return
			}
		}
	})
}
