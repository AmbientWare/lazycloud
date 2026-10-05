-- name: ManagedImage :one
-- The managed image for a version, template and architecture, as workspace
-- sees it.
select i.digest, i.id, i.dockerfile, i.python_version, i.architecture,
       coalesce(w.reference, i.reference) as reference,
       exists (select 1 from image_reference_layers r where r.reference = coalesce(w.reference, i.reference))::bool as converted
from managed_images m
join images i on i.digest = m.image_digest
left join workspace_images w on w.image_digest = i.digest and w.workspace_id = @workspace_id
where m.python_version = @python_version and m.template = @template and m.architecture = @architecture;

-- name: RecordManagedImage :exec
-- The first image recorded for a version, template and architecture stays.
insert into managed_images (python_version, template, architecture, image_digest)
values (@python_version, @template, @architecture, @image_digest)
on conflict do nothing;

-- name: HostArchitecture :one
select architecture from hosts where id = @id;

-- name: ImageDigestOf :one
select digest from images where id = @id;

-- name: ImageRuntime :one
select python_version, architecture from images where id = @id;
