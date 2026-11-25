"""Client for ml-service/ (see ml-service/src/ml_service/main.py)."""

from typing import Any

import httpx

from travelrag.ml.ports import (
    Embedding,
    MLUnavailableError,
    OcrLine,
    SparseVector,
    Transcript,
    TranscriptSegment,
)

EMBED_BATCH = 32


class MLServiceClient:
    """Implements Embedder, Reranker, Transcriber and OcrEngine."""

    def __init__(self, base_url: str, timeout: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def _post(self, path: str, **kwargs: Any) -> Any:
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as http:
                response = await http.post(path, **kwargs)
        except httpx.HTTPError as exc:
            raise MLUnavailableError(
                f"ml-service at {self._base_url} is not reachable ({type(exc).__name__}). "
                "Start it with: cd ml-service && uv run uvicorn ml_service.main:app --port 8100"
            ) from exc
        if response.status_code >= 400:
            raise MLUnavailableError(
                f"ml-service {path} -> {response.status_code}: {response.text}"
            )
        return response.json()

    async def embed(self, texts: list[str]) -> list[Embedding]:
        out: list[Embedding] = []
        for start in range(0, len(texts), EMBED_BATCH):
            data = await self._post("/embed", json={"texts": texts[start : start + EMBED_BATCH]})
            out.extend(
                Embedding(dense=d, sparse=SparseVector(s["indices"], s["values"]))
                for d, s in zip(data["dense"], data["sparse"], strict=True)
            )
        return out

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        data = await self._post("/rerank", json={"query": query, "documents": documents})
        return [float(s) for s in data["scores"]]

    async def transcribe(self, wav_16k_mono: bytes, language: str | None = None) -> Transcript:
        params = {"language": language} if language else {}
        data = await self._post(
            "/transcribe",
            content=wav_16k_mono,
            params=params,
            headers={"Content-Type": "audio/wav"},
        )
        return Transcript(
            language=data.get("language"),
            segments=[
                TranscriptSegment(s["start_ms"], s["end_ms"], s["text"]) for s in data["segments"]
            ],
        )

    async def read(self, images: list[bytes]) -> list[list[OcrLine]]:
        if not images:
            return []
        files = [("files", (f"frame{i}.jpg", img, "image/jpeg")) for i, img in enumerate(images)]
        data = await self._post("/ocr", files=files)
        return [
            [OcrLine(line["text"], float(line["confidence"])) for line in result]
            for result in data["results"]
        ]
