-- Where the customer's creation of a managed authorization's stack stands
-- while the authorization has never validated: waiting for the stack, the
-- stack being created, or the stack failed. Null once it validates, and for
-- an existing role.
alter table cloud_authorizations
    add column setup_step text check (setup_step in ('waiting_for_stack', 'stack_creating', 'stack_failed'));

update cloud_authorizations set setup_step = 'waiting_for_stack'
where mode = 'managed_stack' and last_validated_at is null;
