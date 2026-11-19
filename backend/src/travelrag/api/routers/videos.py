import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from travelrag.api.deps import CurrentUserDep, SessionDep, get_job_queue, resolve_user
from travelrag.api.routers.jobs import JobOut
from travelrag.core.db import get_sessionmaker
from travelrag.ingestion.adapters.local_upload import LocalUploadSource
from travelrag.ingestion.domain import (
    DuplicateVideoError,
    FileTooLargeError,
    UnsupportedVideoTypeError,
    Video,
    VideoNotFoundError,
)
from travelrag.ingestion.factory import build_ingestion_service, build_video_storage
from travelrag.ingestion.service import IngestionService
from travelrag.jobs.domain import Job
from travelrag.jobs.ports import JobQueue

router = APIRouter(prefix="/videos", tags=["videos"])


async def get_ingestion_service(
    session: SessionDep, queue: Annotated[JobQueue, Depends(get_job_queue)]
) -> IngestionService:
    return build_ingestion_service(session, queue)


IngestionDep = Annotated[IngestionService, Depends(get_ingestion_service)]


class VideoOut(BaseModel):
    id: uuid.UUID
    source: str
    original_name: str
    content_type: str | None
    size_bytes: int
    duration_ms: int | None
    width: int | None
    height: int | None
    has_audio: bool | None
    created_at: datetime
    latest_job: JobOut | None

    @classmethod
    def build(cls, video: Video, job: Job | None) -> "VideoOut":
        return cls(
            id=video.id,
            source=video.source.value,
            original_name=video.original_name,
            content_type=video.content_type,
            size_bytes=video.size_bytes,
            duration_ms=video.duration_ms,
            width=video.width,
            height=video.height,
            has_audio=video.has_audio,
            created_at=video.created_at,
            latest_job=JobOut.from_job(job) if job else None,
        )


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Video not found")


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def upload_video(
    file: Annotated[UploadFile, File(description="Video file (.mp4, .mov, .m4v, .webm, .mkv)")],
    user: CurrentUserDep,
    service: IngestionDep,
) -> VideoOut:
    """Upload a video from the device and start its processing job."""
    try:
        result = await service.ingest(user.id, LocalUploadSource(file))
    except UnsupportedVideoTypeError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    except FileTooLargeError as exc:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc)) from exc
    except DuplicateVideoError as exc:
        detail: dict[str, Any] = {
            "message": str(exc),
            "video_id": str(exc.existing_id) if exc.existing_id else None,
        }
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc
    return VideoOut.build(result.video, result.job)


@router.get("")
async def list_videos(user: CurrentUserDep, service: IngestionDep) -> list[VideoOut]:
    return [VideoOut.build(item.video, item.job) for item in await service.list_for_user(user.id)]


@router.get("/{video_id}")
async def get_video(video_id: uuid.UUID, user: CurrentUserDep, service: IngestionDep) -> VideoOut:
    try:
        video = await service.get_for_user(video_id, user.id)
    except VideoNotFoundError as exc:
        raise _not_found() from exc
    return VideoOut.build(video, await service.latest_job(video.id))


@router.delete("/{video_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_video(video_id: uuid.UUID, user: CurrentUserDep, service: IngestionDep) -> None:
    """Delete the video, its file and its jobs."""
    try:
        await service.delete(video_id, user.id)
    except VideoNotFoundError as exc:
        raise _not_found() from exc


@router.get("/{video_id}/file", response_class=FileResponse)
async def video_file(
    video_id: uuid.UUID,
    token: str = Query(description="Access token (<video> elements cannot send headers)"),
) -> FileResponse:
    """Stream the original file (supports Range requests, so players can seek)."""
    async with get_sessionmaker()() as session:
        user = await resolve_user(session, token)
        try:
            video = await build_ingestion_service(session).get_for_user(video_id, user.id)
        except VideoNotFoundError as exc:
            raise _not_found() from exc
    path = build_video_storage().local_path(video.storage_key)
    if not path.exists():
        raise _not_found()
    return FileResponse(path, media_type=video.content_type or "application/octet-stream")
