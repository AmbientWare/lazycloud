package notifications

import (
	"context"
	"errors"
	"fmt"
	"html"
	"strings"

	"github.com/jackc/pgx/v5"
)

// AdminAlerts is where administrator alerts go and the fleet spend that
// raises them. An empty Email sends none; a nil limit alerts on nothing.
type AdminAlerts struct {
	Email             string
	HourlyLimitMicros *int64
	DailyLimitMicros  *int64
}

// AdminAlerts returns the alert settings, empty before an administrator
// sets them.
func (n *Notifications) AdminAlerts(ctx context.Context) (AdminAlerts, error) {
	row, err := n.queries.AdminAlerts(ctx)
	if errors.Is(err, pgx.ErrNoRows) {
		return AdminAlerts{}, nil
	}
	if err != nil {
		return AdminAlerts{}, fmt.Errorf("read admin alerts: %w", err)
	}
	return AdminAlerts(row), nil
}

// SetAdminAlerts replaces the alert settings. Spend already over a changed
// limit alerts on the next check.
func (n *Notifications) SetAdminAlerts(ctx context.Context, a AdminAlerts) error {
	if err := n.queries.SetAdminAlerts(ctx, SetAdminAlertsParams(a)); err != nil {
		return fmt.Errorf("set admin alerts: %w", err)
	}
	return nil
}

// Alert is an administrator alert: its subject, which also heads the body,
// a few labelled facts and a closing note.
type Alert struct {
	Subject string
	Facts   []Fact
	Note    string
}

// Fact is one labelled value; Link, when set, is where the value points.
type Fact struct{ Label, Value, Link string }

// EnqueueAdminAlert queues an alert to the administrators' address in tx;
// nothing when no address is set.
func EnqueueAdminAlert(ctx context.Context, tx pgx.Tx, alert Alert) error {
	row, err := New(tx).AdminAlerts(ctx)
	if errors.Is(err, pgx.ErrNoRows) || err == nil && row.Email == "" {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read admin alerts: %w", err)
	}
	return enqueueAlert(ctx, tx, row.Email, alert)
}

func enqueueAlert(ctx context.Context, tx pgx.Tx, to string, a Alert) error {
	h := html.EscapeString
	var text, body strings.Builder
	text.WriteString(a.Subject + "\n\n")
	body.WriteString(`<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;` +
		`max-width:480px;margin:0 auto;padding:32px 24px;color:#1a1a1a;line-height:1.5">` +
		`<h1 style="font-size:18px;font-weight:600;margin:0 0 20px">` + h(a.Subject) + `</h1>` +
		`<table style="border-collapse:collapse;width:100%;font-size:14px">`)
	for _, f := range a.Facts {
		text.WriteString(f.Label + ": " + f.Value + "\n")
		value := h(f.Value)
		if f.Link != "" {
			value = `<a href="` + h(f.Link) + `" style="color:#1a1a1a">` + value + `</a>`
		}
		body.WriteString(`<tr style="border-top:1px solid #eee"><td style="padding:8px 0;color:#777">` + h(f.Label) +
			`</td><td style="padding:8px 0;text-align:right;font-weight:500">` + value + `</td></tr>`)
	}
	body.WriteString(`</table>`)
	if a.Note != "" {
		text.WriteString("\n" + a.Note + "\n")
		body.WriteString(`<p style="margin:20px 0 0;font-size:13px;color:#888">` + h(a.Note) + `</p>`)
	}
	body.WriteString(`</div>`)
	_, err := Enqueue(ctx, tx, Email{To: to, Subject: a.Subject, HTML: body.String(), Text: text.String()})
	return err
}

// AlertSpend emails the administrators when the platform fleet's spend, per
// hour now and over the last 24 hours, crosses a limit: once per crossing,
// again only after spend falls back under it. Concurrent checks serialize on
// the settings row.
func (n *Notifications) AlertSpend(ctx context.Context, hourlyMicros, dailyMicros int64) error {
	err := pgx.BeginFunc(ctx, n.pool, func(tx pgx.Tx) error {
		q := n.queries.WithTx(tx)
		row, err := q.LockAdminAlerts(ctx)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		over := func(limit *int64, spent int64) bool { return limit != nil && spent > *limit }
		hourly, daily := over(row.HourlyLimitMicros, hourlyMicros), over(row.DailyLimitMicros, dailyMicros)
		for _, c := range []struct {
			crossed bool
			alert   string
			limit   *int64
		}{
			{hourly && !row.HourlyOver, "hourly", row.HourlyLimitMicros},
			{daily && !row.DailyOver, "daily", row.DailyLimitMicros},
		} {
			if !c.crossed || row.Email == "" {
				continue
			}
			if err := enqueueAlert(ctx, tx, row.Email, Alert{
				Subject: "Fleet spend is over your " + c.alert + " limit",
				Facts: []Fact{
					{Label: "Now", Value: dollars(hourlyMicros) + " an hour"},
					{Label: "Last 24 hours", Value: dollars(dailyMicros)},
					{Label: "Limit", Value: dollars(*c.limit)},
				},
				Note: "Change limits under Settings, Admin, Spend.",
			}); err != nil {
				return err
			}
		}
		if hourly == row.HourlyOver && daily == row.DailyOver {
			return nil
		}
		return q.SetSpendOver(ctx, SetSpendOverParams{HourlyOver: hourly, DailyOver: daily})
	})
	if err != nil {
		return fmt.Errorf("alert fleet spend: %w", err)
	}
	return nil
}

// dollars formats USD micros to the cent.
func dollars(micros int64) string {
	cents := (micros + 5_000) / 10_000
	return fmt.Sprintf("$%d.%02d", cents/100, cents%100)
}
