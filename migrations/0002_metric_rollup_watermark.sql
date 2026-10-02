-- Neki's router rejects reads and writes on a table created earlier in the
-- same transaction, and each migration runs in one, so a migration seeds a
-- table it creates in the next file.
insert into container_metric_rollup (rolled_through) values (date_trunc('minute', now() - interval '1 hour'));
