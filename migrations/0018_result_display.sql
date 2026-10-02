-- How a cloudpickle result shows without loading it: the ResultDisplay the
-- runner made of the value where it ran, checked by the host session. The
-- dashboard renders it; nothing on the server unpickles the result.
alter table task_results add column display jsonb;
