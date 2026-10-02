package schedules

import (
	"testing"
	"time"
)

type cronCase struct {
	expression string
	normalized string
	next       []string
	err        string
}

// croniterStarts are the times each case's next occurrences follow.
var croniterStarts = []time.Time{ //nolint:gochecknoglobals // test table
	time.Date(2026, 9, 7, 12, 0, 0, 0, time.UTC),
	time.Date(2026, 12, 31, 23, 59, 30, 0, time.UTC),
	time.Date(2027, 2, 28, 0, 0, 0, 0, time.UTC),
}

// croniterCases record what croniter 6.2.4 computes for the same
// normalization. "0 0 30 2 *" would never fire, so deploy rejects it.
//
//nolint:gochecknoglobals // test table
var croniterCases = []cronCase{
	{expression: "*/15 * * * *", normalized: "*/15 * * * *", next: []string{"2026-09-07T12:15:00Z", "2027-01-01T00:00:00Z", "2027-02-28T00:15:00Z"}},
	{expression: "0 3 * * 1", normalized: "0 3 * * 1", next: []string{"2026-09-14T03:00:00Z", "2027-01-04T03:00:00Z", "2027-03-01T03:00:00Z"}},
	{expression: "@hourly", normalized: "@hourly", next: []string{"2026-09-07T13:00:00Z", "2027-01-01T00:00:00Z", "2027-02-28T01:00:00Z"}},
	{expression: "@daily", normalized: "@daily", next: []string{"2026-09-08T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "@midnight", normalized: "@midnight", next: []string{"2026-09-08T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "@weekly", normalized: "@weekly", next: []string{"2026-09-13T00:00:00Z", "2027-01-03T00:00:00Z", "2027-03-07T00:00:00Z"}},
	{expression: "@monthly", normalized: "@monthly", next: []string{"2026-10-01T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "@yearly", normalized: "@yearly", next: []string{"2027-01-01T00:00:00Z", "2027-01-01T00:00:00Z", "2028-01-01T00:00:00Z"}},
	{expression: "@annually", normalized: "@annually", next: []string{"2027-01-01T00:00:00Z", "2027-01-01T00:00:00Z", "2028-01-01T00:00:00Z"}},
	{expression: "@HOURLY", normalized: "@hourly", next: []string{"2026-09-07T13:00:00Z", "2027-01-01T00:00:00Z", "2027-02-28T01:00:00Z"}},
	{expression: "every 5m", normalized: "*/5 * * * *", next: []string{"2026-09-07T12:05:00Z", "2027-01-01T00:00:00Z", "2027-02-28T00:05:00Z"}},
	{expression: "every 7m", normalized: "*/7 * * * *", next: []string{"2026-09-07T12:07:00Z", "2027-01-01T00:00:00Z", "2027-02-28T00:07:00Z"}},
	{expression: "every 59m", normalized: "*/59 * * * *", next: []string{"2026-09-07T12:59:00Z", "2027-01-01T00:00:00Z", "2027-02-28T00:59:00Z"}},
	{expression: "every 60m", err: "unsupported cron interval: every 60m"},
	{expression: "every 5h", normalized: "0 */5 * * *", next: []string{"2026-09-07T15:00:00Z", "2027-01-01T00:00:00Z", "2027-02-28T05:00:00Z"}},
	{expression: "every 23h", normalized: "0 */23 * * *", next: []string{"2026-09-07T23:00:00Z", "2027-01-01T00:00:00Z", "2027-02-28T23:00:00Z"}},
	{expression: "every 24h", err: "unsupported cron interval: every 24h"},
	{expression: "every 5d", normalized: "0 0 */5 * *", next: []string{"2026-09-11T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "every 31d", normalized: "0 0 */31 * *", next: []string{"2026-10-01T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "every 32d", err: "unsupported cron interval: every 32d"},
	{expression: "every 0m", err: "cron expression must contain exactly five fields"},
	{expression: "every 5 m", err: "cron expression must contain exactly five fields"},
	{expression: "  0   9 * * *  ", normalized: "0 9 * * *", next: []string{"2026-09-08T09:00:00Z", "2027-01-01T09:00:00Z", "2027-02-28T09:00:00Z"}},
	{expression: "0 9 * JAN MON-FRI", normalized: "0 9 * jan mon-fri", next: []string{"2027-01-01T09:00:00Z", "2027-01-01T09:00:00Z", "2028-01-03T09:00:00Z"}},
	{expression: "0 0 L * *", normalized: "0 0 l * *", next: []string{"2026-09-30T00:00:00Z", "2027-01-31T00:00:00Z", "2027-03-31T00:00:00Z"}},
	{expression: "0 0 1,L * *", normalized: "0 0 1,l * *", next: []string{"2026-09-30T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "0 0 * * 1#2", normalized: "0 0 * * 1#2", next: []string{"2026-09-14T00:00:00Z", "2027-01-11T00:00:00Z", "2027-03-08T00:00:00Z"}},
	{expression: "0 0 * * 5#5", normalized: "0 0 * * 5#5", next: []string{"2026-10-30T00:00:00Z", "2027-01-29T00:00:00Z", "2027-04-30T00:00:00Z"}},
	{expression: "0 0 * * 7", normalized: "0 0 * * 7", next: []string{"2026-09-13T00:00:00Z", "2027-01-03T00:00:00Z", "2027-03-07T00:00:00Z"}},
	{expression: "0 0 * * 0-7", normalized: "0 0 * * 0-7", next: []string{"2026-09-08T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "0 0 * * 6-0", normalized: "0 0 * * 6-0", next: []string{"2026-09-12T00:00:00Z", "2027-01-02T00:00:00Z", "2027-03-06T00:00:00Z"}},
	{expression: "5-1 * * * *", normalized: "5-1 * * * *", next: []string{"2026-09-07T12:01:00Z", "2027-01-01T00:00:00Z", "2027-02-28T00:01:00Z"}},
	{expression: "* * * * 5-7", normalized: "* * * * 5-7", next: []string{"2026-09-11T00:00:00Z", "2027-01-01T00:00:00Z", "2027-02-28T00:01:00Z"}},
	{expression: "0 0 1 * 1", normalized: "0 0 1 * 1", next: []string{"2026-09-14T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "0 0 */3 * mon", normalized: "0 0 */3 * mon", next: []string{"2026-09-10T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "0 0 1-5 * mon", normalized: "0 0 1-5 * mon", next: []string{"2026-09-14T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "0 0 1 * */1", normalized: "0 0 1 * */1", next: []string{"2026-09-08T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "0 0 */1 * 1", normalized: "0 0 */1 * 1", next: []string{"2026-09-08T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "0 0 ? * 1", normalized: "0 0 ? * 1", next: []string{"2026-09-14T00:00:00Z", "2027-01-04T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "0 0 1 * *,1", normalized: "0 0 1 * *,1", next: []string{"2026-10-01T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "1-5/2 * * * *", normalized: "1-5/2 * * * *", next: []string{"2026-09-07T12:01:00Z", "2027-01-01T00:01:00Z", "2027-02-28T00:01:00Z"}},
	{expression: "5/15 * * * *", normalized: "5/15 * * * *", next: []string{"2026-09-07T12:05:00Z", "2027-01-01T00:05:00Z", "2027-02-28T00:05:00Z"}},
	{expression: "0 0 29 2 *", normalized: "0 0 29 2 *", next: []string{"2028-02-29T00:00:00Z", "2028-02-29T00:00:00Z", "2028-02-29T00:00:00Z"}},
	{expression: "0 0 31 * *", normalized: "0 0 31 * *", next: []string{"2026-10-31T00:00:00Z", "2027-01-31T00:00:00Z", "2027-03-31T00:00:00Z"}},
	{expression: "0 0 * jan-mar/2 *", normalized: "0 0 * jan-mar/2 *", next: []string{"2027-01-01T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "0 0 * * 1-5/2", normalized: "0 0 * * 1-5/2", next: []string{"2026-09-09T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "0 0 15W * *", normalized: "0 0 15w * *", next: []string{"2026-09-15T00:00:00Z", "2027-01-15T00:00:00Z", "2027-03-15T00:00:00Z"}},
	{expression: "0 0 1-L * *", normalized: "0 0 1-l * *", next: []string{"2026-09-08T00:00:00Z", "2027-01-01T00:00:00Z", "2027-03-01T00:00:00Z"}},
	{expression: "* * * * */2", normalized: "* * * * */2", next: []string{"2026-09-08T00:00:00Z", "2027-01-02T00:00:00Z", "2027-02-28T00:01:00Z"}},
	{expression: "0 22 * * sun-sat", normalized: "0 22 * * sun-sat", next: []string{"2026-09-07T22:00:00Z", "2027-01-01T22:00:00Z", "2027-02-28T22:00:00Z"}},
	{expression: "30 4 1,15 * 5", normalized: "30 4 1,15 * 5", next: []string{"2026-09-11T04:30:00Z", "2027-01-01T04:30:00Z", "2027-03-01T04:30:00Z"}},
	{expression: "0 0 * * fri#3", normalized: "0 0 * * fri#3", next: []string{"2026-09-18T00:00:00Z", "2027-01-15T00:00:00Z", "2027-03-19T00:00:00Z"}},
	{expression: "? * * * *", err: "invalid cron expression: ? * * * *"},
	{expression: "0 0 * * 5L", err: "invalid cron expression: 0 0 * * 5L"},
	{expression: "0 0 * * L", err: "invalid cron expression: 0 0 * * L"},
	{expression: "0 0 L-2 * *", err: "invalid cron expression: 0 0 L-2 * *"},
	{expression: "0 0 * * mon#1,fri", err: "invalid cron expression: 0 0 * * mon#1,fri"},
	{expression: "*/0 * * * *", err: "invalid cron expression: */0 * * * *"},
	{expression: "60 * * * *", err: "invalid cron expression: 60 * * * *"},
	{expression: "0 24 * * *", err: "invalid cron expression: 0 24 * * *"},
	{expression: "0 0 0 * *", err: "invalid cron expression: 0 0 0 * *"},
	{expression: "0 0 * 13 *", err: "invalid cron expression: 0 0 * 13 *"},
	{expression: "0 0 * * 8", err: "invalid cron expression: 0 0 * * 8"},
	{expression: "0 0 1 1 * 2026", err: "cron expression must contain exactly five fields"},
	{expression: "0 0 30 2 *", err: "invalid cron expression: 0 0 30 2 *"},
	{expression: "@reboot", err: "unsupported cron alias: @reboot"},
	{expression: "H * * * *", err: "invalid cron expression: H * * * *"},
	{expression: "", err: "cron expression is required"},
	{expression: "a b c d e", err: "invalid cron expression: a b c d e"},
	{expression: "0 0 * * mon-", err: "invalid cron expression: 0 0 * * mon-"},
}

func TestParseCronMatchesCroniter(t *testing.T) {
	t.Parallel()
	for _, tc := range croniterCases {
		cron, err := ParseCron(tc.expression)
		if tc.err != "" {
			if err == nil || err.Error() != tc.err {
				t.Errorf("ParseCron(%q) error = %v, want %q", tc.expression, err, tc.err)
			}
			continue
		}
		if err != nil {
			t.Errorf("ParseCron(%q): %v", tc.expression, err)
			continue
		}
		if cron.String() != tc.normalized {
			t.Errorf("ParseCron(%q) = %q, want %q", tc.expression, cron.String(), tc.normalized)
		}
		for n, start := range croniterStarts {
			next, ok := cron.Next(start)
			if !ok || next.Format(time.RFC3339) != tc.next[n] {
				t.Errorf("%q after %s = %s (%v), want %s", tc.expression, start.Format(time.RFC3339), next.Format(time.RFC3339), ok, tc.next[n])
			}
		}
	}
}

func TestNextIsStrictlyAfter(t *testing.T) {
	t.Parallel()
	cron, err := ParseCron("*/5 * * * *")
	if err != nil {
		t.Fatal(err)
	}
	at := time.Date(2026, 9, 7, 12, 5, 0, 0, time.UTC)
	if next, _ := cron.Next(at); !next.Equal(at.Add(5 * time.Minute)) {
		t.Fatalf("Next(%s) = %s, want five minutes later", at, next)
	}
}
