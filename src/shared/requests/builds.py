from pydantic import BaseModel, Field


class BuildContextUploadRequest(BaseModel):
    """Request for a pre-signed URL to upload build context."""

    deployment_name: str
    image_name: str
    content_hash: str = Field(
        description="SHA256 hash of the build context for deduplication"
    )


class StartBuildRequest(BaseModel):
    """Request to start a remote build."""

    deployment_name: str
    image_name: str
    dockerfile: str = "Dockerfile"
    context_s3_key: str = Field(description="S3 key where build context was uploaded")
    build_args: dict[str, str] = Field(default_factory=dict)


class BatchStartBuildRequest(BaseModel):
    """Request to start multiple builds in parallel."""

    deployment_name: str
    builds: list[StartBuildRequest]


class BuildStatusRequest(BaseModel):
    """Request to get build status."""

    build_id: str


class BatchBuildStatusRequest(BaseModel):
    """Request to get status of multiple builds."""

    build_ids: list[str]
