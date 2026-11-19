# RAG-traveling

A personal travel map built from **review videos**. Users upload videos, and the system extracts the places mentioned in them (restaurants, temples, hotels, ...) and pins them on the user's own map. From that data, users can **build itineraries** and **ask questions**.

Think of the place reviews on Google Maps. The difference is that each place here is summarized from videos the user uploaded, not from other people's reviews.

> Status: **design phase**, no code yet. The detailed design and roadmap are in [PLAN.md](PLAN.md).

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

## Planned layout

```
src/travelrag/
├── api/          # FastAPI routers, SSE
├── worker/       # worker entrypoint, task registration
├── ingestion/    # upload, VideoSource, job creation
├── media/        # ASR, frame selection, OCR, timestamped chunking
├── extraction/   # LLM → PlaceMention
├── places/       # resolution, aggregation, summary, place cards
├── geo/          # Nominatim, Overpass, OSRM, opening_hours
├── planning/     # preference intake, solver, place replacement
├── qa/           # intent, filters, retrieval, rerank, answer
├── indexing/     # dense + sparse embeddings, trigram
├── llm/          # client, structured output, prompts
├── jobs/         # job state, notifications
└── core/         # config, db, auth, logging
migrations/       # Alembic
tests/
```

## Running

Not yet available. This section will be written in **Phase 0** (see [PLAN.md](PLAN.md#7-roadmap)). The plan is a single `docker compose up` that starts Postgres, api, worker and OSRM.
