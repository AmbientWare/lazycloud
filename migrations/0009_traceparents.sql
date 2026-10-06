-- The W3C traceparent of the span that asked for each piece of work, when
-- that span was sampled, so the steps other processes take later join its
-- trace: a container's placement, start and image pull; an image build's
-- run and publish; a platform image's conversion; a release's rollout.
alter table containers add column traceparent text;
alter table image_builds add column traceparent text;
alter table platform_images add column traceparent text;
alter table releases add column traceparent text;
