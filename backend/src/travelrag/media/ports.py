from pathlib import Path
from typing import Protocol

from travelrag.media.domain import VideoMetadata


class VideoProber(Protocol):
    async def probe(self, path: Path) -> VideoMetadata:
        """Read container metadata. Raises InvalidVideoError if the file is not decodable."""
        ...
