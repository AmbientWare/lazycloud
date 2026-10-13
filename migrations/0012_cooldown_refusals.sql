-- How long the pool's last cooldown lasted: a refusal after it ended cools
-- the pool twice as long. A launch there deletes the row.
alter table capacity_cooldowns add column cooled_for interval not null default interval '0';

-- The disk slots a reserve was bought with, which every pool its launch
-- moves to must offer.
alter table hosts add column holds_disks smallint;
