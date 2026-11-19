# RAG-traveling

A personal travel map built from **review videos**. Users upload videos, and the system extracts the places mentioned in them (restaurants, temples, hotels, ...) and pins them on the user's own map. From that data, users can **build itineraries** and **ask questions**.

Think of the place reviews on Google Maps. The difference is that each place here is summarized from videos the user uploaded, not from other people's reviews.

> Status: **Phase 0 done, Phase 1 in progress**: auth, job queue, live job progress, local video upload with probing, test console. The detailed design and roadmap are in [PLAN.md](PLAN.md).

## Features

1. **Extract places from videos.** Upload local videos (TikTok links come later). The system runs ASR + OCR, extracts information with an LLM against a fixed schema, resolves places against OSM, and merges what several videos say about the same place into one *place card*.
2. **Personal trip planning.** The app asks for dates, area, budget and preferences, then proposes a day-by-day itinerary. Travel times and opening hours are handled by a solver + OSRM. The LLM only proposes places and explains its choices. Users can remove or replace places, and add places from a video while editing a plan.
3. **Q&A (RAG).** Searches only the user's own data. There are two scopes: the whole map, or a single place. Every answer cites its sources (video + timestamp).

## Architecture at a glance

- A **modular monolith** in Python. One codebase runs as two processes:
  - `api` (FastAPI): lightweight, loads no ML models.
  - `worker` (procrastinate): processes videos asynchronously and loads Whisper, OCR, bge-m3 and the reranker.
- Each module follows **ports & adapters**: `domain` → `service` → `ports` (Protocol) → `adapters`.
- **One database:** PostgreSQL + pgvector + pg_trgm + unaccent. The job queue also runs on Postgres.

## Stack

| Component | Choice |
|---|---|
| Language / packaging | Python 3.12, `uv` |
| API | FastAPI, Pydantic v2, SSE for job progress |
| DB / ORM / migrations | PostgreSQL 16 + pgvector (≥ 0.7, for `sparsevec`), pg_trgm, unaccent · SQLAlchemy 2 · Alembic |
| Job queue | procrastinate (runs on Postgres) |
| ASR | faster-whisper |
| OCR | PySceneDetect + imagehash (frame selection), PaddleOCR |
| Embeddings | bge-m3 (dense + sparse) |
| Reranker | bge-reranker-v2-m3 |
| Geo | Nominatim + Overpass (OSM), OSRM (routing), `opening_hours` parser |
| Scheduling | OR-Tools (routing with time windows) |
| Fuzzy name matching | rapidfuzz |
| LLM | A provider with solid structured-output support (not decided yet, see PLAN.md) |

## Layout

```
backend/                  # Python package `travelrag` (uv project)
├── src/travelrag/
│   ├── api/              # FastAPI app, routers, SSE                     ✅ Phase 0
│   ├── worker/           # procrastinate app + task registration         ✅ Phase 0
│   ├── jobs/             # job state machine, queue, events              ✅ Phase 0
│   ├── core/             # config, db, security, users, logging          ✅ Phase 0
│   ├── ingestion/        # upload, VideoSource, storage, process_video   ✅ 1.1
│   ├── media/            # probe ✅ · ASR, frames, OCR, chunking          Phase 1
│   ├── extraction/       # LLM → PlaceMention                            Phase 1
│   ├── places/           # resolution, aggregation, summary, cards       Phase 1
│   ├── geo/              # Nominatim, Overpass, OSRM, opening_hours      Phase 1
│   ├── indexing/         # dense + sparse embeddings, trigram            Phase 2
│   ├── qa/               # intent, filters, retrieval, rerank, answer    Phase 2
│   ├── planning/         # preferences, solver, replacement              Phase 3
│   └── llm/              # client, structured output, prompts            Phase 1
├── migrations/           # Alembic
└── tests/                # unit/ (no I/O) and integration/ (real Postgres)
frontend/                 # Vue 3 + Vite. For now a single test console page
docker/                   # Postgres init scripts
scripts/                  # osrm-prepare.sh
docker-compose.yml
```

## Running

Requirements: Docker (Docker Desktop, Colima, ...). For local development outside Docker you also need [uv](https://docs.astral.sh/uv/) and Node 22+.

```bash
docker compose up --build
```

| Service | URL | Notes |
|---|---|---|
| `web` | http://localhost:5173 | Test console: register/log in, start dummy jobs, watch progress live |
| `api` | http://localhost:8000/docs | OpenAPI docs. Auto-reloads on changes to `backend/src` |
| `worker` | — | Restart it after code changes: `docker compose restart worker` |
| `db` | `localhost:5432` | `travelrag` / `travelrag`. Databases: `travelrag` (app), `travelrag_test` (tests) |
| `migrate` | — | Runs `alembic upgrade head` once, before api/worker start |

In the console, **Videos** uploads a local file (`.mp4 .mov .m4v .webm .mkv`, up to 500 MB and 20 min by default). This creates a `process_video` job, which currently probes the file (duration, resolution, audio) and finishes. Later stages are added in Phase 1.2+. Uploaded files live in the `media` Docker volume.

The dummy job walks through the pipeline stages (`asr → ocr → extract → resolve → index`). Its options let you exercise every job state: `fail_at` ends in `failed` (then **Retry**), and `needs_review` ends in `needs_review` (then **Approve**).

### OSRM (optional)

```bash
./scripts/osrm-prepare.sh [PBF_URL]      # default: Vietnam extract from Geofabrik
docker compose --profile geo up osrm     # http://localhost:5001
```

### Backend without Docker

```bash
cd backend
cp .env.example .env
uv sync
uv run alembic upgrade head
uv run uvicorn travelrag.api.main:app --reload             # API
uv run procrastinate --app=travelrag.worker.app.app worker  # worker (separate terminal)
```

Postgres must still be running (`docker compose up db`).

### Frontend without Docker

```bash
cd frontend
npm install
npm run dev        # proxies /api to http://localhost:8000 (override with API_PROXY_TARGET)
```

### Checks

```bash
cd backend
uv run ruff check . && uv run ruff format --check .
uv run mypy
TRAVELRAG_TEST_DATABASE_URL=postgresql+psycopg://travelrag:travelrag@localhost:5432/travelrag_test \
  uv run pytest          # without the variable, integration tests are skipped
```

Integration tests apply migrations automatically and truncate the job queue, so only point them at a disposable database. CI (`.github/workflows/ci.yml`) runs all of the above plus the frontend build. Pre-commit hooks: `uv tool install pre-commit && pre-commit install`.
