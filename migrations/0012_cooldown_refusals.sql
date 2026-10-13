-- How many times in a row a pool was refused: each refusal on a retry soon
-- after its last cooldown ended cools the pool longer. A launch there
-- deletes the row.
alter table capacity_cooldowns add column refusals smallint not null default 1;
