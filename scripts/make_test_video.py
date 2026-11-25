"""Make a short test video with Vietnamese speech (macOS `say`) and on-screen text.

    cd backend && uv run python ../scripts/make_test_video.py out.mp4 \
        --say "Hôm nay mình ăn phở Thìn Lò Đúc, giá sáu mươi lăm nghìn một tô." \
        --overlay "PHỞ THÌN" --overlay "13 LÒ ĐÚC"

Exercises the real ASR + OCR path of the pipeline without filming anything.
"""

import argparse
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

import av
from PIL import Image, ImageDraw, ImageFont

FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FPS = 5


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=Path)
    parser.add_argument("--say", required=True, help="text spoken by the macOS voice")
    parser.add_argument("--voice", default="Linh", help="macOS voice (Linh = Vietnamese)")
    parser.add_argument("--overlay", action="append", default=[], help="line of on-screen text")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        speech = Path(tmp) / "speech.aiff"
        subprocess.run(["say", "-v", args.voice, "-o", str(speech), args.say], check=True)

        image = Image.new("RGB", (640, 360), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(FONT, 44)
        for i, line in enumerate(args.overlay):
            draw.text((40, 100 + i * 70), line, fill="black", font=font)

        with av.open(str(speech)) as src, av.open(str(args.output), "w") as out:
            audio_in = src.streams.audio[0]
            seconds = float(audio_in.duration * audio_in.time_base) if audio_in.duration else 10.0
            video = out.add_stream("mpeg4", rate=FPS)
            video.width, video.height, video.pix_fmt = 640, 360, "yuv420p"
            audio = out.add_stream("aac", rate=16000)
            frame = av.VideoFrame.from_image(image).reformat(format="yuv420p")
            for i in range(int(seconds * FPS) + 1):
                frame.pts, frame.time_base = i, Fraction(1, FPS)
                out.mux(video.encode(frame))
            out.mux(video.encode())
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
            for chunk in src.decode(audio=0):
                for resampled in resampler.resample(chunk):
                    resampled.pts = None
                    out.mux(audio.encode(resampled))
            out.mux(audio.encode())
    print(f"wrote {args.output} ({seconds:.1f} s)")


if __name__ == "__main__":
    main()
