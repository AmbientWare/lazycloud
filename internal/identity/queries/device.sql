-- name: InsertDeviceCode :one
-- A user code collision inserts nothing, and the caller draws another.
insert into device_codes (device_code_hash, user_code, client_name, status, poll_interval_seconds, expires_at)
values (@device_code_hash, @user_code, @client_name, 'pending', @poll_interval_seconds, @expires_at)
on conflict (user_code) do nothing
returning id;

-- name: DeviceCodeByUserCode :one
select id, user_code, client_name, status, created_at, expires_at, consumed_at
from device_codes where user_code = @user_code;

-- name: DecideDeviceCode :one
-- Only a pending, unexpired code is decided.
update device_codes
set status = @status, user_id = sqlc.narg(user_id), decided_at = now()
where user_code = @user_code and status = 'pending' and expires_at > now()
returning id, user_code, client_name, status, created_at, expires_at, consumed_at;

-- name: LockDeviceCodeByHash :one
select id, client_name, status, user_id, poll_interval_seconds, last_polled_at, expires_at, consumed_at, now()::timestamptz as now
from device_codes where device_code_hash = @device_code_hash for update;

-- name: RecordDevicePoll :exec
update device_codes set last_polled_at = now(), poll_interval_seconds = @poll_interval_seconds where id = @id;

-- name: ConsumeDeviceCode :exec
update device_codes set consumed_at = now(), last_polled_at = now() where id = @id;

-- name: PruneDeviceCodes :execrows
-- Expired codes stay an hour so a late poll still learns it expired.
delete from device_codes
where id in (
    select p.id from device_codes p where p.expires_at < now() - interval '1 hour'
    order by p.expires_at limit @row_limit
);
