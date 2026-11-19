from collections.abc import AsyncIterator
from typing import Protocol

from travelrag.ingestion.domain import VideoSourceKind
from travelrag.ingestion.ports import IncomingVideo

CHUNK_SIZE = 1024 * 1024


class _UploadedFile(Protocol):
    """What we need from an uploaded file (Starlette's UploadFile satisfies it)."""

    filename: str | None
    content_type: str | None

    async def read(self, size: int = -1) -> bytes: ...


class LocalUploadSource:
    """A video uploaded from the user's device."""

    kind = VideoSourceKind.LOCAL

    def __init__(self, upload: _UploadedFile) -> None:
        self._upload = upload

    async def open(self) -> IncomingVideo:
        return IncomingVideo(
            filename=self._upload.filename or "upload",
            content_type=self._upload.content_type,
            chunks=self._chunks(),
        )

    async def _chunks(self) -> AsyncIterator[bytes]:
        while chunk := await self._upload.read(CHUNK_SIZE):
            yield chunk
