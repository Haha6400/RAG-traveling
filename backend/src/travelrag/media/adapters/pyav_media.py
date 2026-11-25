"""Audio extraction and frame sampling with PyAV (+ Pillow for frame images)."""

import asyncio
import io
import wave
from pathlib import Path

import av
from PIL import Image

from travelrag.media.domain import SampledFrame

SAMPLE_RATE = 16_000
HASH_DIFF_THRESHOLD = 10  # of 64 bits; above this a frame counts as a new scene
MAX_FRAMES = 120
MAX_SIDE = 960


def dhash(image: Image.Image) -> int:
    """64-bit difference hash: robust to small changes, differs across scene cuts."""
    small = image.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    px = list(small.tobytes())  # mode "L": one byte per pixel
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | int(px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _extract_wav(path: Path) -> bytes | None:
    with av.open(str(path)) as container:
        if not container.streams.audio:
            return None
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        pcm = bytearray()
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                pcm.extend(out.to_ndarray().tobytes())
        for out in resampler.resample(None):
            pcm.extend(out.to_ndarray().tobytes())
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(pcm))
    return buffer.getvalue()


def _sample_frames(path: Path, every_seconds: float) -> list[SampledFrame]:
    """One frame every `every_seconds`, keeping only frames that differ from the last kept
    one (dHash). This keeps scene changes and drops near-duplicates in a single pass."""
    kept: list[SampledFrame] = []
    last_hash: int | None = None
    next_ts = 0.0
    with av.open(str(path)) as container:
        if not container.streams.video:
            return []
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        for frame in container.decode(stream):
            if frame.time is None or frame.time < next_ts:
                continue
            next_ts = frame.time + every_seconds
            image = frame.to_image()  # type: ignore[no-untyped-call]
            h = dhash(image)
            if last_hash is not None and hamming(h, last_hash) <= HASH_DIFF_THRESHOLD:
                continue
            last_hash = h
            image.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            image.convert("RGB").save(out, format="JPEG", quality=85)
            kept.append(SampledFrame(int(frame.time * 1000), out.getvalue()))
            if len(kept) >= MAX_FRAMES:
                break
    return kept


class PyAVMediaReader:
    async def extract_wav(self, path: Path) -> bytes | None:
        return await asyncio.to_thread(_extract_wav, path)

    async def sample_frames(self, path: Path, every_seconds: float) -> list[SampledFrame]:
        return await asyncio.to_thread(_sample_frames, path, every_seconds)
