import asyncio
from pathlib import Path

import av

from travelrag.media.domain import InvalidVideoError, VideoMetadata


def _probe_sync(path: Path) -> VideoMetadata:
    try:
        with av.open(str(path)) as container:
            video = container.streams.video[0] if container.streams.video else None
            duration_ms = None
            if container.duration is not None:
                duration_ms = int(container.duration * 1000 / av.time_base)
            elif video is not None and video.duration is not None and video.time_base:
                duration_ms = int(video.duration * video.time_base * 1000)
            return VideoMetadata(
                duration_ms=duration_ms,
                width=video.codec_context.width if video else None,
                height=video.codec_context.height if video else None,
                has_video=video is not None,
                has_audio=bool(container.streams.audio),
            )
    except av.FFmpegError as exc:
        # str(exc) includes the absolute file path; keep only the reason (it reaches users).
        raise InvalidVideoError(exc.strerror or "unreadable media") from exc


class PyAVVideoProber:
    """Probes with PyAV (bundled FFmpeg libraries, no system ffmpeg needed)."""

    async def probe(self, path: Path) -> VideoMetadata:
        return await asyncio.to_thread(_probe_sync, path)
