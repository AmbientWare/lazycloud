-- The image reference a container was started with, recorded as its start
-- is sent. Its host may read that reference's converted layers while the
-- container is live; layer grant refreshes list them from here.
alter table containers add column image_reference text;
