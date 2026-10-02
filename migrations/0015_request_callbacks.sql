-- Endpoint and ASGI requests of a release with a callback_url are called
-- back once each ends, as the reference called back the task it made per
-- request. The edge writes the outbox row in the transaction that records
-- the request, and callbacks delivers it like a task's.
alter table task_callbacks
    alter column task_id drop not null,
    add column request_id uuid references http_requests (id) on delete cascade,
    add constraint task_callbacks_one_subject check (num_nonnulls(task_id, request_id) = 1),
    add constraint task_callbacks_request unique (request_id);
