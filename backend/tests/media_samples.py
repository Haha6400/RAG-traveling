"""Generate tiny real video files for tests (no binary fixtures in the repo)."""

from fractions import Fraction
from pathlib import Path

import av


def make_video(
    path: Path, *, seconds: float = 1.0, with_audio: bool = True, width: int = 64, height: int = 48
) -> Path:
    fps = 10
    with av.open(str(path), mode="w") as container:
        video = container.add_stream("mpeg4", rate=fps)
        video.width, video.height = width, height
        video.pix_fmt = "yuv420p"
        audio = container.add_stream("aac", rate=16000) if with_audio else None

        for i in range(int(seconds * fps)):
            frame = av.VideoFrame(width, height, "yuv420p")
            for plane in frame.planes:
                plane.update(bytes([(i * 20) % 256]) * plane.buffer_size)
            frame.pts = i
            frame.time_base = Fraction(1, fps)
            container.mux(video.encode(frame))
        container.mux(video.encode())

        if audio is not None:
            samples, pts = 1024, 0
            while pts < seconds * 16000:
                frame = av.AudioFrame(format="fltp", layout="mono", samples=samples)
                frame.planes[0].update(bytes(frame.planes[0].buffer_size))
                frame.sample_rate = 16000
                frame.pts = pts
                container.mux(audio.encode(frame))
                pts += samples
            container.mux(audio.encode())
    return path
