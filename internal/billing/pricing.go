package billing

import (
	"fmt"
	"math/big"
	"time"

	"github.com/AmbientWare/lazycloud/internal/cpu"
)

const (
	bytesPerGiB        = int64(1) << 30
	microsPerHour      = int64(3_600_000_000)
	millisPerCPU       = int64(1_000)
	microsecondsPerSec = int64(1_000_000)
)

// Shape is what the control plane placed, which is what prices: never what
// a host says it ran.
type Shape struct {
	Owner       BillingOwner
	Class       RateClass
	GPU         GPUType
	GPUCount    int
	CPUMillis   cpu.Millis
	MemoryBytes int64
}

// Charge is one interval's price, per component. Each component rounds to
// whole nanodollars on its own, half to even, so a component's total on the
// usage page is the sum of what the ledger holds.
type Charge struct {
	Version        string
	ContainerNanos int64
	CPUNanos       int64
	MemoryNanos    int64
	GPUNanos       int64
	// StoredBytes and AttachedBytes are what a storage source held over the
	// interval; StorageNanos and AttachedNanos are their price.
	StoredBytes   int64
	AttachedBytes int64
	StorageNanos  int64
	AttachedNanos int64
	// EgressBytes left for the internet; EgressNanos is their price.
	EgressBytes int64
	EgressNanos int64
}

// Total is the interval's cost.
func (c Charge) Total() int64 {
	return c.ContainerNanos + c.CPUNanos + c.MemoryNanos + c.GPUNanos + c.StorageNanos + c.AttachedNanos + c.EgressNanos
}

// price is the cost of holding shape for d under card. The arithmetic is
// exact: an hourly rate times the resource times microseconds, divided once.
func (c RateCard) price(shape Shape, d time.Duration) (Charge, error) {
	gpu := shape.GPU
	switch {
	case shape.GPUCount == 0:
		gpu = noGPU
	case gpu == noGPU:
		// A card nobody named the model of would price as free.
		return Charge{}, fmt.Errorf("a container holds %d GPUs of no recorded model", shape.GPUCount)
	}
	rate, err := c.computeRate(shape.Owner, shape.Class, gpu)
	if err != nil {
		return Charge{}, err
	}
	micros := d.Microseconds()
	return Charge{
		Version:        c.Version,
		ContainerNanos: cost(rate.ContainerHour, 1, 1, micros),
		CPUNanos:       cost(rate.CPUHour, int64(shape.CPUMillis), millisPerCPU, micros),
		MemoryNanos:    cost(rate.MemoryGiBHour, shape.MemoryBytes, bytesPerGiB, micros),
		GPUNanos:       cost(rate.GPUCardHour, int64(shape.GPUCount), 1, micros),
	}, nil
}

// storageCost is bytes held for d at a GiB-month rate, a month being the
// published 30 days.
func storageCost(gibMonth, bytes int64, d time.Duration) int64 {
	return costOver(gibMonth, bytes, bytesPerGiB, d.Microseconds(), storageMonthSeconds*microsecondsPerSec)
}

// cost is hourly × units/perUnit × micros/microsPerHour, rounded half to
// even.
func cost(hourly, units, perUnit, micros int64) int64 {
	return costOver(hourly, units, perUnit, micros, microsPerHour)
}

// costOver is rate × units/perUnit × micros/periodMicros, exactly, rounded
// half to even.
func costOver(rate, units, perUnit, micros, periodMicros int64) int64 {
	if rate == 0 || units == 0 || micros == 0 {
		return 0
	}
	num := new(big.Int).Mul(big.NewInt(rate), big.NewInt(units))
	num.Mul(num, big.NewInt(micros))
	den := new(big.Int).Mul(big.NewInt(perUnit), big.NewInt(periodMicros))
	q, r := new(big.Int).QuoRem(num, den, new(big.Int))
	switch new(big.Int).Lsh(r, 1).Cmp(den) {
	case 1:
		q.Add(q, big.NewInt(1))
	case 0:
		if q.Bit(0) == 1 {
			q.Add(q, big.NewInt(1))
		}
	}
	return q.Int64()
}
