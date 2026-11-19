"""Adapter tests that touch only the local filesystem."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.media_samples import make_video
from travelrag.ingestion.adapters.local_storage import LocalVideoStorage
from travelrag.ingestion.domain import FileTooLargeError
from travelrag.media.adapters.pyav_prober import PyAVVideoProber
from travelrag.media.domain import InvalidVideoError


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def test_probe_real_video(tmp_path: Path) -> None:
    meta = await PyAVVideoProber().probe(make_video(tmp_path / "v.mp4", seconds=2))
    assert meta.has_video and meta.has_audio
    assert (meta.width, meta.height) == (64, 48)
    assert meta.duration_ms is not None and 1900 <= meta.duration_ms <= 2200


async def test_probe_video_without_audio(tmp_path: Path) -> None:
    meta = await PyAVVideoProber().probe(make_video(tmp_path / "v.mp4", with_audio=False))
    assert meta.has_video and not meta.has_audio


async def test_probe_garbage_hides_server_path(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"definitely not a video" * 50)
    with pytest.raises(InvalidVideoError) as info:
        await PyAVVideoProber().probe(bad)
    assert str(tmp_path) not in str(info.value)


async def test_storage_saves_with_hash_and_size(tmp_path: Path) -> None:
    storage = LocalVideoStorage(tmp_path)
    stored = await storage.save("u/v.mp4", _chunks(b"abc", b"def"), max_bytes=100)
    assert stored.size_bytes == 6
    # sha256("abcdef")
    assert stored.sha256 == "bef57ec7f53a6d40beb640a780a639c83bc29ac8a9816f1fc6c5c6dcd93c4721"
    assert storage.local_path("u/v.mp4").read_bytes() == b"abcdef"
    await storage.delete("u/v.mp4")
    await storage.delete("u/v.mp4")  # idempotent
    assert not storage.local_path("u/v.mp4").exists()


async def test_storage_rejects_oversized_and_leaves_nothing(tmp_path: Path) -> None:
    storage = LocalVideoStorage(tmp_path)
    with pytest.raises(FileTooLargeError):
        await storage.save("u/v.mp4", _chunks(b"x" * 60, b"x" * 60), max_bytes=100)
    assert not storage.local_path("u/v.mp4").exists()
    assert list((tmp_path / ".tmp").iterdir()) == []


def test_storage_rejects_keys_escaping_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        LocalVideoStorage(tmp_path / "media").local_path("../secret")
