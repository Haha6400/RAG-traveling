# ml-service

Local model server used by the backend (api + worker):

| Endpoint | Model | Used for |
|---|---|---|
| `POST /embed` `{texts}` | `BAAI/bge-m3`: dense (1024-d, normalised CLS) + sparse (lexical weights) | indexing, query embedding |
| `POST /rerank` `{query, documents}` | `BAAI/bge-reranker-v2-m3` (sigmoid scores 0–1) | Q&A reranking |
| `POST /transcribe` (body: 16 kHz mono 16-bit WAV, `?language=`) | faster-whisper (`small` by default) | ASR |
| `POST /ocr` (multipart `files`) | EasyOCR (`vi`, `en`) | on-screen text |
| `GET /health` | — | device and model names |

The bge-m3 sparse head is implemented directly with `transformers`, following the reference
implementation: relu(linear(hidden)) per token, max-pooled per token id, special tokens
dropped. This avoids the FlagEmbedding dependency.

## Run

It runs natively rather than in Docker, so it can use the Apple GPU (MPS) and the host's RAM.
The models need about 5 GB.

```bash
cd ml-service
uv sync
uv run uvicorn ml_service.main:app --host 127.0.0.1 --port 8100
```

Models load lazily on first use (downloads go to `~/.cache/huggingface`). The api/worker
containers reach the service at `http://host.docker.internal:8100`. Outside Docker the backend
uses `http://127.0.0.1:8100` (`TRAVELRAG_ML_SERVICE_URL`).

## Configuration (environment)

| Variable | Default |
|---|---|
| `ML_DEVICE` | auto: `mps` → `cuda` → `cpu` |
| `ML_EMBED_MODEL` / `ML_RERANK_MODEL` | `BAAI/bge-m3` / `BAAI/bge-reranker-v2-m3` |
| `ML_WHISPER_MODEL` | `small` (use `large-v3` or `turbo` for better Vietnamese) |
| `ML_OCR_LANGS` | `vi,en` |
| `ML_MAX_LENGTH`, `ML_BATCH_SIZE` | `1024`, `16` |

faster-whisper (CTranslate2) runs on the CPU on macOS. Requests are serialised with a lock, so
GPU memory use stays predictable.
