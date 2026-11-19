import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from travelrag.ingestion.domain import FileTooLargeError
from travelrag.ingestion.ports import StoredFile


class LocalVideoStorage:
    """Stores files under a root directory. Writes go to a temp file first and are moved into
    place only when complete, so readers never see partial files."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def local_path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            raise ValueError(f"storage key escapes the media root: {key!r}")
        return path

    async def save(self, key: str, chunks: AsyncIterator[bytes], max_bytes: int) -> StoredFile:
        final = self.local_path(key)
        tmp_dir = self._root / ".tmp"
        await asyncio.to_thread(tmp_dir.mkdir, parents=True, exist_ok=True)
        tmp = tmp_dir / uuid.uuid4().hex

        digest = hashlib.sha256()
        size = 0
        try:
            with tmp.open("wb") as handle:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise FileTooLargeError(max_bytes)
                    digest.update(chunk)
                    await asyncio.to_thread(handle.write, chunk)
            await asyncio.to_thread(final.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(os.replace, tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
        return StoredFile(size_bytes=size, sha256=digest.hexdigest())

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.local_path(key).unlink, missing_ok=True)
