from compute.block_volumes import BlockVolume, BlockVolumeOwner, BlockVolumeState, orphaned_volumes


def _volume(
    volume_id: str,
    *,
    disk_id: str = "disk",
    deployment: str | None = "ours",
    attached_to: str = "",
) -> BlockVolume:
    return BlockVolume(
        volume_id=volume_id,
        zone="use2-az1",
        size_bytes=1024**3,
        state=BlockVolumeState.InUse if attached_to else BlockVolumeState.Available,
        attached_instance_id=attached_to,
        owner=(
            BlockVolumeOwner(deployment=deployment, workspace_id="workspace", disk_id=disk_id)
            if deployment is not None
            else None
        ),
    )


def test_orphan_collection_takes_only_this_deployments_unrecorded_detached_volumes() -> None:
    listed = (
        _volume("vol-untagged", deployment=None, disk_id="gone"),
        _volume("vol-foreign", deployment="staging", disk_id="gone"),
        _volume("vol-attached", disk_id="gone", attached_to="i-a"),
        _volume("vol-current", disk_id="live"),
        _volume("vol-creating", disk_id="making"),
        _volume("vol-no-row", disk_id="gone"),
        _volume("vol-superseded", disk_id="live"),
    )
    orphans = orphaned_volumes(
        listed,
        deployment="ours",
        recorded={"live": "vol-current", "making": ""},
        creating=frozenset({"making"}),
    )
    assert [volume.volume_id for volume in orphans] == ["vol-no-row", "vol-superseded"]
