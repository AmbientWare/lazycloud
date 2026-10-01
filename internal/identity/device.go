package identity

import (
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"math/big"
	"strings"
	"time"
	"unicode"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// Device-code login timing, after RFC 8628.
const (
	DeviceCodeTTL        = 15 * time.Minute
	DevicePollInterval   = 5 * time.Second
	devicePollSlowDown   = 5 * time.Second
	userCodeAttempts     = 5
	clientNameMaxRunes   = 120
	devicePrefix         = "dc_"
	userCodeAlphabet     = "BCDFGHJKLMNPQRSTVWXZ"
	userCodeGroupLength  = 4
	defaultDeviceClient  = "cli"
	deviceStatusApproved = "approved"
	deviceStatusDenied   = "denied"
)

// DeviceStatus is where a device-code login stands.
type DeviceStatus string

const (
	// DevicePending waits for the person to approve or deny.
	DevicePending DeviceStatus = "pending"
	// DeviceSlowDown tells a client that polled early to wait longer.
	DeviceSlowDown DeviceStatus = "slow_down"
	// DeviceApproved: the poll that saw it received the token.
	DeviceApproved DeviceStatus = "approved"
	// DeviceDenied: the person refused the login.
	DeviceDenied DeviceStatus = "denied"
	// DeviceExpired: nobody decided in time.
	DeviceExpired DeviceStatus = "expired"
)

// DeviceStart is a new device-code login. DeviceCode stays with the
// client; UserCode is what the person types at /activate.
type DeviceStart struct {
	DeviceCode string
	UserCode   string
	ExpiresIn  time.Duration
	Interval   time.Duration
}

// StartDeviceLogin opens a device-code login for a client such as
// "cli@laptop".
func (i *Identity) StartDeviceLogin(ctx context.Context, clientName string) (DeviceStart, error) {
	name := strings.TrimSpace(clientName)
	if runes := []rune(name); len(runes) > clientNameMaxRunes {
		name = string(runes[:clientNameMaxRunes])
	}
	if name == "" {
		name = defaultDeviceClient
	}
	code, digest, err := newSecret(devicePrefix)
	if err != nil {
		return DeviceStart{}, err
	}
	for range userCodeAttempts {
		userCode, err := newUserCode()
		if err != nil {
			return DeviceStart{}, err
		}
		_, err = i.queries.InsertDeviceCode(ctx, InsertDeviceCodeParams{
			DeviceCodeHash: digest, UserCode: userCode, ClientName: name,
			PollIntervalSeconds: int32(DevicePollInterval / time.Second), ExpiresAt: time.Now().Add(DeviceCodeTTL),
		})
		if errors.Is(err, pgx.ErrNoRows) {
			continue // The user code is taken; draw another.
		}
		if err != nil {
			return DeviceStart{}, fmt.Errorf("insert device code: %w", err)
		}
		return DeviceStart{DeviceCode: code, UserCode: userCode, ExpiresIn: DeviceCodeTTL, Interval: DevicePollInterval}, nil
	}
	return DeviceStart{}, &ConflictError{Message: "could not allocate a unique user code; try again"}
}

// newUserCode draws XXXX-XXXX from consonants that are unambiguous to read
// aloud and type.
func newUserCode() (string, error) {
	var b strings.Builder
	limit := big.NewInt(int64(len(userCodeAlphabet)))
	for n := range 2 * userCodeGroupLength {
		if n == userCodeGroupLength {
			b.WriteByte('-')
		}
		index, err := rand.Int(rand.Reader, limit)
		if err != nil {
			return "", fmt.Errorf("draw user code: %w", err)
		}
		b.WriteByte(userCodeAlphabet[index.Int64()])
	}
	return b.String(), nil
}

// NormalizeUserCode accepts a user code typed with any case, spacing or
// dash and returns XXXX-XXXX.
func NormalizeUserCode(value string) (string, error) {
	var compact []rune
	for _, r := range strings.ToUpper(value) {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			compact = append(compact, r)
		}
	}
	if len(compact) != 2*userCodeGroupLength {
		return "", &InvalidError{Message: "the code has eight letters, such as BCDF-GHJK"}
	}
	return string(compact[:userCodeGroupLength]) + "-" + string(compact[userCodeGroupLength:]), nil
}

// DeviceCode is a device-code login as the approving person sees it.
type DeviceCode struct {
	UserCode   string
	ClientName string
	Status     DeviceStatus
	CreatedAt  time.Time
	ExpiresAt  time.Time
}

// DeviceLogin reads the login a user code names.
func (i *Identity) DeviceLogin(ctx context.Context, userCode string) (DeviceCode, error) {
	code, err := NormalizeUserCode(userCode)
	if err != nil {
		return DeviceCode{}, err
	}
	row, err := i.queries.DeviceCodeByUserCode(ctx, code)
	if errors.Is(err, pgx.ErrNoRows) {
		return DeviceCode{}, ErrNotFound
	}
	if err != nil {
		return DeviceCode{}, fmt.Errorf("read device code: %w", err)
	}
	return deviceCodeOut(row), nil
}

func deviceCodeOut(row DeviceCodeByUserCodeRow) DeviceCode {
	status := DeviceStatus(row.Status)
	if status == DevicePending && !row.ExpiresAt.After(time.Now()) {
		status = DeviceExpired
	}
	return DeviceCode{
		UserCode: row.UserCode, ClientName: row.ClientName, Status: status,
		CreatedAt: row.CreatedAt, ExpiresAt: row.ExpiresAt,
	}
}

// ApproveDeviceLogin grants the waiting client a token for the caller's
// account. A token restricted to one workspace cannot approve, because the
// device token reaches every workspace of the account.
func (i *Identity) ApproveDeviceLogin(ctx context.Context, p Principal, userCode string) (DeviceCode, error) {
	if err := p.requireAccount("approve a device sign-in"); err != nil {
		return DeviceCode{}, err
	}
	user := uuid.UUID(p.User)
	return i.decideDeviceLogin(ctx, userCode, deviceStatusApproved, &user)
}

// DenyDeviceLogin refuses the waiting client.
func (i *Identity) DenyDeviceLogin(ctx context.Context, userCode string) (DeviceCode, error) {
	return i.decideDeviceLogin(ctx, userCode, deviceStatusDenied, nil)
}

func (i *Identity) decideDeviceLogin(ctx context.Context, userCode, status string, user *uuid.UUID) (DeviceCode, error) {
	code, err := NormalizeUserCode(userCode)
	if err != nil {
		return DeviceCode{}, err
	}
	row, err := i.queries.DecideDeviceCode(ctx, DecideDeviceCodeParams{Status: status, UserID: user, UserCode: code})
	if err == nil {
		return deviceCodeOut(DeviceCodeByUserCodeRow(row)), nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return DeviceCode{}, fmt.Errorf("decide device code: %w", err)
	}
	current, err := i.DeviceLogin(ctx, code)
	if err != nil {
		return DeviceCode{}, err
	}
	if current.Status == DeviceExpired {
		return DeviceCode{}, &ConflictError{Message: "the code has expired; run lazycloud login again"}
	}
	return DeviceCode{}, &ConflictError{Message: fmt.Sprintf("the code was already %s", current.Status)}
}

// DevicePoll is one poll's answer. Token is set once, on the poll that
// finds the login approved.
type DevicePoll struct {
	Status   DeviceStatus
	Token    string
	Interval time.Duration
}

// PollDeviceLogin answers a client polling with its device code. Polling
// sooner than the interval returns slow_down and lengthens it by five
// seconds. The approving poll mints a device token named after the client
// and consumes the code, in one transaction.
func (i *Identity) PollDeviceLogin(ctx context.Context, deviceCode string) (DevicePoll, error) {
	var poll DevicePoll
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		row, err := q.LockDeviceCodeByHash(ctx, HashToken(deviceCode))
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("lock device code: %w", err)
		}
		interval := time.Duration(row.PollIntervalSeconds) * time.Second
		poll = DevicePoll{Interval: interval}
		if row.ConsumedAt != nil {
			return &ConflictError{Message: "the device code was already used"}
		}
		if !row.ExpiresAt.After(row.Now) {
			poll.Status = DeviceExpired
			return nil
		}
		if row.LastPolledAt != nil && row.Now.Sub(*row.LastPolledAt) < interval {
			poll.Status = DeviceSlowDown
			poll.Interval = interval + devicePollSlowDown
			if err := q.RecordDevicePoll(ctx, RecordDevicePollParams{
				PollIntervalSeconds: int32(poll.Interval / time.Second), ID: row.ID, //nolint:gosec // Bounded by the code's lifetime.
			}); err != nil {
				return fmt.Errorf("record device poll: %w", err)
			}
			return nil
		}
		switch row.Status {
		case deviceStatusApproved:
			if row.UserID == nil {
				return errors.New("approved device code has no user")
			}
			token, _, err := mintToken(ctx, q, UserID(*row.UserID), row.ClientName, nil, true)
			if err != nil {
				return err
			}
			poll.Status, poll.Token = DeviceApproved, token
		case deviceStatusDenied:
			poll.Status = DeviceDenied
		default:
			poll.Status = DevicePending
			if err := q.RecordDevicePoll(ctx, RecordDevicePollParams{PollIntervalSeconds: row.PollIntervalSeconds, ID: row.ID}); err != nil {
				return fmt.Errorf("record device poll: %w", err)
			}
			return nil
		}
		if err := q.ConsumeDeviceCode(ctx, row.ID); err != nil {
			return fmt.Errorf("consume device code: %w", err)
		}
		return nil
	})
	if err != nil {
		return DevicePoll{}, fmt.Errorf("poll device login: %w", err)
	}
	return poll, nil
}
