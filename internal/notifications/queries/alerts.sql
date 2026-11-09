-- name: AdminAlerts :one
-- Locks the settings row, so spend checks serialize on it.
select email, hourly_limit_micros, daily_limit_micros, hourly_over, daily_over from admin_alerts for update;

-- name: SetAdminAlerts :exec
-- A changed limit sends again when spend is over it.
insert into admin_alerts (email, hourly_limit_micros, daily_limit_micros)
values (@email, sqlc.narg(hourly_limit_micros), sqlc.narg(daily_limit_micros))
on conflict (singleton) do update set
    email = excluded.email,
    hourly_limit_micros = excluded.hourly_limit_micros,
    daily_limit_micros = excluded.daily_limit_micros,
    hourly_over = admin_alerts.hourly_over and admin_alerts.hourly_limit_micros is not distinct from excluded.hourly_limit_micros,
    daily_over = admin_alerts.daily_over and admin_alerts.daily_limit_micros is not distinct from excluded.daily_limit_micros,
    updated_at = now();

-- name: SetSpendOver :exec
update admin_alerts set hourly_over = @hourly_over, daily_over = @daily_over;
