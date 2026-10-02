-- Endpoint and ASGI requests of a release with a callback_url are called
-- back once each ends, as the reference called back the task it made per
-- request. The edge writes the outbox row in the transaction that records
-- the request, and callbacks delivers it like a task's. release_id is the
-- request's release: at most a bounded number of its callbacks wait at once,
-- and the edge records the rest as failed with the reason.
alter table task_callbacks
    alter column task_id drop not null,
    add column request_id uuid references http_requests (id) on delete cascade,
    add column release_id uuid references releases (id) on delete cascade,
    add constraint task_callbacks_one_subject check (num_nonnulls(task_id, request_id) = 1),
    add constraint task_callbacks_request unique (request_id),
    add constraint task_callbacks_request_release check ((request_id is null) = (release_id is null));

-- Pending request callbacks per release, which the cap counts.
create index task_callbacks_pending_release on task_callbacks (release_id) where state = 'pending' and release_id is not null;
-- Due callbacks per workspace, which claims take in turn.
create index task_callbacks_due_workspace on task_callbacks (workspace_id, next_attempt_at, id) where state = 'pending';
