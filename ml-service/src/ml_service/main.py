"""HTTP API. Run natively (to use the Apple GPU):

    uv run uvicorn ml_service.main:app --host 127.0.0.1 --port 8100
"""

import asyncio
import logging
from typing import Annotated, Any

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from ml_service import models

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="RAG-traveling ML service", version="0.1.0")


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=256)


class SparseOut(BaseModel):
    indices: list[int]
    values: list[float]


class EmbedResponse(BaseModel):
    dense: list[list[float]]
    sparse: list[SparseOut]
    sparse_dim: int


class RerankRequest(BaseModel):
    query: str
    documents: list[str] = Field(min_length=1, max_length=256)


class RerankResponse(BaseModel):
    scores: list[float]


async def _run(fn: Any, *args: Any) -> Any:
    """Run a blocking model call in a thread, one at a time."""

    def call() -> Any:
        with models.locked():
            return fn(*args)

    return await asyncio.to_thread(call)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "device": models.DEVICE,
        "models": {
            "embed": models.EMBED_MODEL,
            "rerank": models.RERANK_MODEL,
            "whisper": models.WHISPER_MODEL,
            "ocr": models.OCR_LANGS,
        },
    }


@app.post("/embed")
async def embed(body: EmbedRequest) -> EmbedResponse:
    model = await asyncio.to_thread(models.bge_m3)
    dense, sparse = await _run(model.encode, body.texts)
    return EmbedResponse(
        dense=dense,
        sparse=[SparseOut(indices=s.indices, values=s.values) for s in sparse],
        sparse_dim=model.vocab_size,
    )


@app.post("/rerank")
async def rerank(body: RerankRequest) -> RerankResponse:
    model = await asyncio.to_thread(models.reranker)
    return RerankResponse(scores=await _run(model.score, body.query, body.documents))


@app.post("/transcribe")
async def transcribe(
    request: Request, language: Annotated[str | None, Query()] = None
) -> dict[str, Any]:
    """Body: 16 kHz mono 16-bit PCM WAV."""
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty body")
    model = await asyncio.to_thread(models.transcriber)
    try:
        result: dict[str, Any] = await _run(model.transcribe, data, language)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


@app.post("/ocr")
async def ocr(files: Annotated[list[UploadFile], File()]) -> dict[str, Any]:
    model = await asyncio.to_thread(models.ocr)
    results = []
    for upload in files:
        results.append(await _run(model.read, await upload.read()))
    return {"results": results}
