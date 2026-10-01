package schedules

import (
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// InvalidCronError rejects an expression deploy cannot schedule. Its message
// is the one users see.
type InvalidCronError struct {
	Message string
}

func (e *InvalidCronError) Error() string { return e.Message }

// Cron is a parsed expression. Times are UTC and minute-precise. The syntax
// and matching follow croniter, which the reference used.
type Cron struct {
	expression string
	minutes    [60]bool
	hours      [24]bool
	months     [13]bool
	days       dayMatcher
	weekdays   weekdayMatcher
	// dayOr is set when both day fields are restricted; a day then matches
	// either, as in Vixie cron.
	dayOr bool
}

// String is the normalized expression stored on releases and shown to users.
func (c Cron) String() string { return c.expression }

var intervalPattern = regexp.MustCompile(`^every\s+([1-9][0-9]*)([mhd])$`) //nolint:gochecknoglobals // compiled once

// alias expands a lowercase @alias.
func alias(name string) (string, bool) {
	switch name {
	case "@annually", "@yearly":
		return "0 0 1 1 *", true
	case "@monthly":
		return "0 0 1 * *", true
	case "@weekly":
		return "0 0 * * 0", true
	case "@daily", "@midnight":
		return "0 0 * * *", true
	case "@hourly":
		return "0 * * * *", true
	}
	return "", false
}

// intervalLimit is the largest `every N` for a unit.
func intervalLimit(unit string) int {
	switch unit {
	case "m":
		return 59
	case "h":
		return 23
	}
	return 31
}

// monthName and weekdayName read three-letter lowercase names.
func monthName(name string) (int, bool) {
	n := strings.Index("janfebmaraprmayjunjulaugsepoctnovdec", name)
	if len(name) != 3 || n < 0 || n%3 != 0 {
		return 0, false
	}
	return n/3 + 1, true
}

func weekdayName(name string) (int, bool) {
	n := strings.Index("sunmontuewedthufrisat", name)
	if len(name) != 3 || n < 0 || n%3 != 0 {
		return 0, false
	}
	return n / 3, true
}

func noNames(string) (int, bool) { return 0, false }

// searchYears bounds how far Next looks for a match. A satisfiable
// expression matches within 28 years, the period of weekdays over dates.
const searchYears = 28

// ParseCron normalizes and parses a schedule: five cron fields, an alias
// such as @hourly, or `every N` minutes, hours or days. Normalization
// lowercases, collapses whitespace and rewrites intervals as cron fields,
// as the reference did. An expression that can never fire is invalid.
func ParseCron(expression string) (Cron, error) {
	normalized := strings.ToLower(strings.Join(strings.Fields(expression), " "))
	if normalized == "" {
		return Cron{}, &InvalidCronError{Message: "cron expression is required"}
	}
	fields := normalized
	if m := intervalPattern.FindStringSubmatch(normalized); m != nil {
		n, err := strconv.Atoi(m[1])
		if err != nil || n > intervalLimit(m[2]) {
			return Cron{}, &InvalidCronError{Message: "unsupported cron interval: " + expression}
		}
		switch m[2] {
		case "m":
			normalized = fmt.Sprintf("*/%d * * * *", n)
		case "h":
			normalized = fmt.Sprintf("0 */%d * * *", n)
		case "d":
			normalized = fmt.Sprintf("0 0 */%d * *", n)
		}
		fields = normalized
	} else if strings.HasPrefix(normalized, "@") {
		expanded, ok := alias(normalized)
		if !ok {
			return Cron{}, &InvalidCronError{Message: "unsupported cron alias: " + normalized}
		}
		fields = expanded
	}
	parts := strings.Split(fields, " ")
	if len(parts) != 5 {
		return Cron{}, &InvalidCronError{Message: "cron expression must contain exactly five fields"}
	}
	invalid := &InvalidCronError{Message: "invalid cron expression: " + expression}
	// `?` stands for any day, so only the day fields accept it.
	if strings.Contains(parts[0]+parts[1]+parts[3], "?") {
		return Cron{}, invalid
	}
	c := Cron{expression: normalized}
	if err := setRange(c.minutes[:], parts[0], 0, 59, noNames); err != nil {
		return Cron{}, invalid
	}
	if err := setRange(c.hours[:], parts[1], 0, 23, noNames); err != nil {
		return Cron{}, invalid
	}
	if err := setRange(c.months[:], parts[3], 1, 12, monthName); err != nil {
		return Cron{}, invalid
	}
	var err error
	if c.days, err = parseDays(parts[2]); err != nil {
		return Cron{}, invalid
	}
	if c.weekdays, err = parseWeekdays(parts[4]); err != nil {
		return Cron{}, invalid
	}
	c.dayOr = !unrestricted(parts[2]) && !unrestricted(parts[4])
	if _, ok := c.Next(time.Date(2000, 1, 1, 0, 0, 0, 0, time.UTC)); !ok {
		return Cron{}, invalid
	}
	return c, nil
}

// unrestricted reports a day field that matches every day: croniter treats
// a `*` or `?` item that way, while `*/1` still counts as a restriction.
func unrestricted(field string) bool {
	for item := range strings.SplitSeq(field, ",") {
		if item == "*" || item == "?" {
			return true
		}
	}
	return false
}

var errField = errors.New("invalid cron field")

// setRange marks the values a list of `*`, `n`, `a-b` and their `/step`
// forms selects. A reversed range wraps past the field's end, as in
// croniter. names reads lowercase names as values.
func setRange(set []bool, field string, low, high int, names func(string) (int, bool)) error {
	size := high - low + 1
	for item := range strings.SplitSeq(field, ",") {
		from, to, step, err := parseItem(item, low, high, names)
		if err != nil {
			return err
		}
		if from > to {
			to += size
		}
		for v := from; v <= to; v += step {
			set[low+(v-low)%size] = true
		}
	}
	return nil
}

// parseItem parses one list item into a range, possibly reversed, and a step.
func parseItem(item string, low, high int, names func(string) (int, bool)) (from, to, step int, err error) {
	base, stepText, hasStep := strings.Cut(item, "/")
	step = 1
	if hasStep {
		if step, err = strconv.Atoi(stepText); err != nil || step < 1 {
			return 0, 0, 0, errField
		}
	}
	switch {
	case base == "*" || base == "?":
		return low, high, step, nil
	case strings.Contains(base, "-"):
		a, b, _ := strings.Cut(base, "-")
		if from, err = value(a, low, high, names); err != nil {
			return 0, 0, 0, err
		}
		if to, err = value(b, low, high, names); err != nil {
			return 0, 0, 0, err
		}
		return from, to, step, nil
	}
	if from, err = value(base, low, high, names); err != nil {
		return 0, 0, 0, err
	}
	// `a/n` runs from a to the field's end, as in croniter.
	if hasStep {
		return from, high, step, nil
	}
	return from, from, step, nil
}

func value(text string, low, high int, names func(string) (int, bool)) (int, error) {
	if v, ok := names(text); ok {
		return v, nil
	}
	v, err := strconv.Atoi(text)
	if err != nil || v < low || v > high {
		return 0, errField
	}
	return v, nil
}

// dayMatcher is the day-of-month field: days 1-31 and `l`, the month's last
// day.
type dayMatcher struct {
	days [32]bool
	last bool
}

func (d dayMatcher) match(t time.Time) bool {
	return d.days[t.Day()] || (d.last && t.AddDate(0, 0, 1).Day() == 1)
}

func parseDays(field string) (dayMatcher, error) {
	var d dayMatcher
	for item := range strings.SplitSeq(field, ",") {
		switch {
		case item == "l":
			d.last = true
		case strings.HasSuffix(item, "-l"):
			// `1-l` runs to the month's last day.
			from, err := value(strings.TrimSuffix(item, "-l"), 1, 31, noNames)
			if err != nil {
				return d, err
			}
			for v := from; v <= 31; v++ {
				d.days[v] = true
			}
		case strings.HasSuffix(item, "w"):
			// croniter accepts `15w` and fires on that day.
			v, err := value(strings.TrimSuffix(item, "w"), 1, 31, noNames)
			if err != nil {
				return d, err
			}
			d.days[v] = true
		default:
			if err := setRange(d.days[:], item, 1, 31, noNames); err != nil {
				return d, err
			}
		}
	}
	return d, nil
}

// weekdayMatcher is the day-of-week field: 0-7 with Sunday as 0 and 7, and
// `d#n`, the month's nth such weekday.
type weekdayMatcher struct {
	weekdays [7]bool
	nth      [7][6]bool
}

func (w weekdayMatcher) match(t time.Time) bool {
	day := int(t.Weekday())
	return w.weekdays[day] || w.nth[day][(t.Day()-1)/7+1]
}

func parseWeekdays(field string) (weekdayMatcher, error) {
	var w weekdayMatcher
	items := strings.Split(field, ",")
	for _, item := range items {
		if day, nth, ok := strings.Cut(item, "#"); ok {
			// croniter accepts `#` only as the field's sole item.
			if len(items) > 1 {
				return w, errField
			}
			d, err := value(day, 0, 7, weekdayName)
			if err != nil {
				return w, err
			}
			n, err := strconv.Atoi(nth)
			if err != nil || n < 1 || n > 5 {
				return w, errField
			}
			w.nth[d%7][n] = true
			continue
		}
		var days [8]bool
		if err := setRange(days[:], item, 0, 7, weekdayName); err != nil {
			return w, err
		}
		for d, set := range days {
			w.weekdays[d%7] = w.weekdays[d%7] || set
		}
	}
	return w, nil
}

func (c Cron) dayMatches(t time.Time) bool {
	if !c.months[int(t.Month())] {
		return false
	}
	if c.dayOr {
		return c.days.match(t) || c.weekdays.match(t)
	}
	return c.days.match(t) && c.weekdays.match(t)
}

// Next is the first whole minute after `after` that the expression matches.
// It reports false when none follows within searchYears.
func (c Cron) Next(after time.Time) (time.Time, bool) {
	t := after.UTC().Truncate(time.Minute).Add(time.Minute)
	limit := t.AddDate(searchYears, 0, 0)
	for t.Before(limit) {
		if !c.dayMatches(t) {
			t = time.Date(t.Year(), t.Month(), t.Day()+1, 0, 0, 0, 0, time.UTC)
			continue
		}
		if c.hours[t.Hour()] {
			for m := t.Minute(); m < 60; m++ {
				if c.minutes[m] {
					return time.Date(t.Year(), t.Month(), t.Day(), t.Hour(), m, 0, 0, time.UTC), true
				}
			}
		}
		t = time.Date(t.Year(), t.Month(), t.Day(), t.Hour()+1, 0, 0, 0, time.UTC)
	}
	return time.Time{}, false
}
