package billing

import (
	"context"
	"fmt"
	"math"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"
)

// periodKey is one container's metering period.
type periodKey struct {
	container uuid.UUID
	period    time.Time
}

// chargeMeasuredUse reprices the closed container entries of plans whose
// measured CPU or memory exceeded the reservation: CPU and memory bill at
// the greater of the two, while a container's time and GPUs bill as
// reserved. One query reads every container's use.
func (b *Billing) chargeMeasuredUse(ctx context.Context, plans []sourcePlan) error {
	var ids []uuid.UUID
	var starts, ends []time.Time
	for _, p := range plans {
		if p.src.kind != kindContainer || len(p.entries) == 0 {
			continue
		}
		ids = append(ids, p.src.id)
		starts = append(starts, p.entries[0].start)
		ends = append(ends, p.entries[len(p.entries)-1].end)
	}
	if len(ids) == 0 {
		return nil
	}
	rows, err := b.queries.MeasuredContainerUse(ctx, MeasuredContainerUseParams{
		ContainerIds: ids, StartAts: starts, EndAts: ends, Period: pgtype.Interval{Microseconds: MeteringPeriod.Microseconds(), Valid: true},
	})
	if err != nil {
		return fmt.Errorf("read measured container use: %w", err)
	}
	used := make(map[periodKey]MeasuredContainerUseRow, len(rows))
	for _, r := range rows {
		used[periodKey{r.ContainerID, r.Period.UTC()}] = r
	}
	for n := range plans {
		p := &plans[n]
		if p.src.kind != kindContainer {
			continue
		}
		if err := b.applyMeasuredUse(p, used); err != nil {
			return err
		}
	}
	return nil
}

// applyMeasuredUse reprices p's entries from what each period measured. A
// period's use is shared among its entries, which a rate change can split,
// by their length.
func (b *Billing) applyMeasuredUse(p *sourcePlan, used map[periodKey]MeasuredContainerUseRow) error {
	covered := map[time.Time]time.Duration{}
	for _, e := range p.entries {
		covered[e.start.UTC().Truncate(MeteringPeriod)] += e.end.Sub(e.start)
	}
	for n := range p.entries {
		e := &p.entries[n]
		period := e.start.UTC().Truncate(MeteringPeriod)
		use, ok := used[periodKey{p.src.id, period}]
		if !ok {
			continue
		}
		length := e.end.Sub(e.start)
		share := float64(length) / float64(covered[period])
		seconds := length.Seconds()
		billed := p.src.shape
		billed.CPUMillis = max(billed.CPUMillis, int64(math.Ceil(use.CoreSeconds*share*float64(millicoresPerCore)/seconds)))
		billed.MemoryBytes = max(billed.MemoryBytes, int64(math.Ceil(use.MemoryByteSeconds*share/seconds)))
		if billed == p.src.shape {
			continue
		}
		card, err := b.rates.cardAt(e.start)
		if err != nil {
			return err
		}
		charge, err := card.price(billed, length)
		if err != nil {
			return err
		}
		e.charge, e.billed = charge, &billed
	}
	return nil
}
