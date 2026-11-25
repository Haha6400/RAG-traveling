# RAG-traveling

A personal travel map built from **review videos**. Users upload videos, and the system extracts the places mentioned in them (restaurants, temples, hotels, ...) and pins them on the user's own map. From that data, users can **build itineraries** and **ask questions**.

Think of the place reviews on Google Maps. The difference is that each place here is summarized from videos the user uploaded, not from other people's reviews.

> Status: **Phases 0–3 implemented** (video → places, Q&A, trip planning), with a Vue app whose home page is the personal map ([docs](docs/frontend-map.md)). Phase 4 (TikTok links, adding places from a video while editing a plan, scaling) is next. The design and roadmap are in [PLAN.md](PLAN.md); how each phase works and how to test it is in [docs/](docs/).

## Features

1. **Extract places from videos** ([docs](docs/phase-1-video-pipeline.md)). Upload local videos (TikTok links come later). The system runs ASR + OCR, extracts information with Claude against a fixed schema, resolves places against OSM (asking "Is this X?" when unsure), and merges what several videos say about the same place into one *place card* with rule-based aggregation and conflict flags.
2. **Q&A (RAG)** ([docs](docs/phase-2-qa.md)). Searches only the user's own data, over the whole map or a single place. Metadata filters run first, then hybrid retrieval (bge-m3 dense + sparse + trigram, RRF) and a cross-encoder rerank. Every answer cites its sources (video + timestamp). Plan questions read the trip state, and distances come from the router.
3. **Personal trip planning** ([docs](docs/phase-3-planning.md)). Dates, area, budget, pace and priorities in; a day-by-day itinerary out. Opening hours, travel time, meals, stops and budget are hard constraints solved with OR-Tools. The LLM only writes notes and picks between replacements that already fit. Stops can be replaced, locked or removed.

## Architecture at a glance

- A **modular monolith** in Python. One codebase runs as two processes:
  - `api` (FastAPI)
  - `worker` (procrastinate): processes videos asynchronously
- **ml-service** (`ml-service/`): a small model server run natively on the host so it can use the Apple GPU. It serves bge-m3, bge-reranker-v2-m3, faster-whisper and EasyOCR. api and worker call it over HTTP.
- Each module follows **ports & adapters**: `domain` → `service` → `ports` (Protocol) → `adapters`.
- **One database:** PostgreSQL + pgvector (dense `vector` + `sparsevec`) + pg_trgm. The job queue also runs on Postgres.

## Stack

| Component | Choice |
|---|---|
| Language / packaging | Python 3.12, `uv` |
| API | FastAPI, Pydantic v2, SSE for job progress |
| DB / ORM / migrations | PostgreSQL 16 + pgvector 0.8 (HNSW on `vector` and `sparsevec`), pg_trgm · SQLAlchemy 2.1 · Alembic |
| Job queue | procrastinate (runs on Postgres) |
| LLM | Claude (`claude-opus-5-5`) via the Anthropic SDK, structured outputs, server-side refusal fallback |
| ASR | faster-whisper (in ml-service) |
| OCR | PyAV frame sampling + dHash de-duplication, EasyOCR (in ml-service) |
| Embeddings / reranker | bge-m3 (dense + sparse) / bge-reranker-v2-m3 (in ml-service) |
| Geo | Nominatim + Overpass (OSM), OSRM (optional; straight-line estimates otherwise), own `opening_hours` parser |
| Scheduling | OR-Tools (prize-collecting VRP with time windows) |
| Fuzzy name matching | rapidfuzz + Vietnamese-aware normalisation |
| Frontend | Vue 3 + Vite + vue-router; Leaflet with OSM raster tiles for the map (MapLibre GL + OpenFreeMap planned, see [docs/frontend-map.md](docs/frontend-map.md)) |

## Layout

```
backend/                  # Python package `travelrag` (uv project)
├── src/travelrag/
│   ├── api/              # FastAPI app, routers (auth, jobs, videos, places, ask, trips), SSE
│   ├── worker/           # procrastinate app + tasks
│   ├── jobs/             # job state machine, queue, events
│   ├── core/             # config, db, security, users, text normalisation
│   ├── ingestion/        # upload, VideoSource, storage, the process_video pipeline
│   ├── media/            # probe, audio, frame sampling, chunking
│   ├── extraction/       # LLM → PlaceMention
│   ├── places/           # resolution, review, aggregation, summary, cards
│   ├── indexing/         # dense + sparse embeddings
│   ├── qa/               # interpretation, filters, hybrid retrieval, answers, eval
│   ├── planning/         # preferences, OR-Tools solver, replacement
│   ├── geo/              # Nominatim / Overpass, OSRM / estimates, opening_hours
│   ├── llm/              # Claude adapter (structured output)
│   ├── ml/               # ml-service client
│   └── demo/             # Hanoi demo dataset, offline fakes, seed command
├── migrations/           # Alembic
└── tests/                # unit/ (no I/O) and integration/ (real Postgres, fake models)
ml-service/               # native model server (uv project)
frontend/                 # Vue 3 app: map (home), videos, ask, trips, dev tools
docs/                     # per-phase logic + testing
docker/  scripts/         # Postgres init; OSRM prep, test-video generator
docker-compose.yml
```

## Running

Requirements: Docker (Docker Desktop, Colima, ...), [uv](https://docs.astral.sh/uv/), and about 5 GB of free RAM for the models.

```bash
# 1. Model server (native, Apple GPU). First calls download the models.
cd ml-service && uv sync && uv run uvicorn ml_service.main:app --host 127.0.0.1 --port 8100

# 2. Claude key (optional but needed for extraction, summaries, notes, answers)
echo 'TRAVELRAG_ANTHROPIC_API_KEY=sk-ant-...' >> .env      # repo root, read by docker compose

# 3. Everything else
docker compose up --build

# 4. Optional: Hanoi demo data, no videos needed
docker compose exec worker python -m travelrag.demo.seed --email demo@example.com --password demo-pass-123
```

| Service | URL | Notes |
|---|---|---|
| `web` | http://localhost:5173 | The app: Map (home), Videos, Ask, Trips; `/dev` has the job tools |
| `api` | http://localhost:8000/docs | OpenAPI docs. Auto-reloads on changes to `backend/src` |
| `worker` | — | Restart it after code changes: `docker compose restart worker` |
| `db` | `127.0.0.1:5432` | `travelrag` / `travelrag`. Databases: `travelrag` (app), `travelrag_test` (tests). With Colima, use `127.0.0.1` rather than `localhost` (IPv4 only) |
| `migrate` | — | Runs `alembic upgrade head` once, before api/worker start |
| ml-service | http://127.0.0.1:8100/health | Native; containers use `host.docker.internal:8100` |

What to try, and what each step should show: [docs/testing.md](docs/testing.md).

### OSRM (optional)

```bash
./scripts/osrm-prepare.sh [PBF_URL]      # default: Vietnam extract from Geofabrik
docker compose --profile geo up -d osrm  # http://localhost:5001
echo 'TRAVELRAG_OSRM_URL=http://osrm:5000' >> .env && docker compose up -d api worker
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
uv run python -m travelrag.qa.eval --email demo@example.com   # retrieval quality (seeded demo)
```

Tests never call Claude, ml-service, Nominatim or OSRM (see [docs/testing.md](docs/testing.md)). Integration tests apply migrations automatically and truncate the job queue, so only point them at a disposable database. CI (`.github/workflows/ci.yml`) runs all of the above plus the frontend build. Pre-commit hooks: `uv tool install pre-commit && pre-commit install`.
