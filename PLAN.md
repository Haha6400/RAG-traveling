# PLAN — RAG-traveling

Design and roadmap. README.md is the short introduction. This file is the main reference during implementation.

## Contents
1. [Goals and scope](#1-goals-and-scope)
2. [Decisions](#2-decisions)
3. [Architecture](#3-architecture)
4. [Data model](#4-data-model)
5. [Feature design](#5-feature-design)
6. [Retrieval](#6-retrieval)
7. [Roadmap](#7-roadmap)
8. [Open questions and risks](#8-open-questions-and-risks)

---

## 1. Goals and scope

Three features, all built on one shared core: each user's **personal map**.

```
Video ──► Transcript + OCR ──► PlaceMention (place × video) ──► Place (OSM id)
                                                                   │
                              ┌────────────────────────────────────┼──────────────────┐
                              ▼                                    ▼                  ▼
                     Place card (aggregated)           Itinerary (constraints)     RAG Q&A
```

**Guiding principle:** the LLM handles language. Anything that has to be exactly right is done in code.

| LLM does | Code does (deterministic) |
|---|---|
| Schema extraction, description summaries, intent classification, filter extraction, picking and explaining suggestions | Place merging, numeric aggregation, distances and travel times, opening hours, scheduling, plan queries |

**Out of scope for the MVP:** importing videos from TikTok or other online links (added later through the `VideoSource` adapter), sharing maps between users, a mobile app.

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | The whole backend is **Python** | Python has the best ecosystem for the ML parts (ASR, OCR, embeddings, rerank), so no sidecar is needed |
| D2 | **Modular monolith**, one codebase running as two processes, `api` and `worker` | Simple for an MVP. Splitting workers by queue later is a deployment change only |
| D3 | Each module follows **ports & adapters** internally | Merge logic, confidence thresholds and the solver can be tested without calling OSM or an LLM. Providers are easy to swap |
| D4 | Video processing runs **asynchronously** on a job queue, with status tracking and notifications | Processing one video takes from tens of seconds to several minutes |
| D5 | **Local upload only** at first, TikTok later | Lower risk (yt-dlp breaks often, ToS concerns) |
| D6 | ASR uses **faster-whisper**. OCR takes frames at scene cuts plus periodic samples, and drops duplicate frames | Most videos have no captions. On-screen text carries shop names and prices |
| D7 | Place merging uses **confidence thresholds**: auto-merge / ask the user / create new | Avoids merging two different shops by mistake |
| D8 | Geo data comes from **OSM**: Nominatim + Overpass, OSRM | Avoids Google Places ToS limits on storing data |
| D9 | Retrieval uses **bge-m3 dense + bge-m3 sparse + pg_trgm**, fused with RRF, then reranked | Works for Vietnamese and English without Vietnamese word segmentation. pg_trgm + unaccent catch proper nouns typed without diacritics or misspelled. Needs only one database |
| D10 | The queue is **procrastinate** (runs on Postgres) | No Redis needed. Jobs and data share transactions |

## 3. Architecture

### 3.1 Processes

```
┌──────────────┐   enqueue    ┌───────────────────────────┐
│  api         │ ───────────► │  Postgres                 │
│  (FastAPI)   │ ◄─ NOTIFY ── │  - application data       │
│  SSE events  │              │  - procrastinate queue    │
└──────────────┘              │  - pgvector / pg_trgm     │
       ▲                      └───────────────────────────┘
       │ live Q&A needs embedding/rerank   ▲
       │ (load small models or call         │ read jobs, write results
       │  a model server, see §8)           │
                              ┌───────────────────────────┐
                              │  worker                   │
                              │  faster-whisper, PaddleOCR│
                              │  bge-m3, reranker, LLM    │
                              └───────────────────────────┘
                                         │
                              Nominatim / Overpass / OSRM
```

### 3.2 Modules

| Module | Responsibility |
|---|---|
| `ingestion` | Accept uploads, store files, create `video` + `job`. Defines the `VideoSource` interface (`LocalUpload` now, `TikTokSource` later) |
| `media` | ASR, frame selection, OCR, timestamped chunking |
| `extraction` | Call the LLM to produce a list of `PlaceMention` (Pydantic schema) |
| `places` | Resolution (matching against OSM and existing places), aggregation, summary, place cards |
| `geo` | Clients for Nominatim, Overpass and OSRM, plus the `opening_hours` parser |
| `planning` | Preference intake, candidate selection, solver, place replacement, adding places to a plan from a video |
| `qa` | Intent → filters → retrieval → rerank → answer generation |
| `indexing` | Compute dense + sparse embeddings, update indexes when chunks or place cards change |
| `llm` | Shared client, structured output, versioned prompts |
| `jobs` | Job state machine and event publishing |
| `core` | Config, DB session, auth, logging |

### 3.3 Inside a module

```
places/
├── domain.py      # pure-Python entities + rules (Place, PlaceMention, AggregationPolicy)
├── service.py     # use cases: resolve_mention(), rebuild_card()
├── ports.py       # typing.Protocol: GeoProvider, Embedder, LLM, PlaceRepository
└── adapters/      # osm.py, pg_repo.py ...
```

**Dependency rules:**
- `domain` imports only the stdlib and pydantic.
- `service` depends only on `domain` + `ports`.
- `adapters` implement `ports`.
- Modules call each other only through the other module's `service`. They never import each other's `adapters`.

### 3.4 Events between modules

| Event | Emitted by | Handled by |
|---|---|---|
| `VideoUploaded` | ingestion | enqueue the video-processing pipeline |
| `MentionsExtracted` | extraction | resolution |
| `PlaceUpdated` | places | rerun aggregation, re-index the place card, mark the summary stale |
| `MatchNeedsReview` | places | move the job to `needs_review`, notify the user |

## 4. Data model

```sql
users(id, email, created_at)

videos(id, user_id, source,            -- 'local' | 'tiktok' (later)
       storage_key, original_name, content_type, size_bytes,
       sha256,                         -- UNIQUE (user_id, sha256): re-uploads are rejected
       duration_ms, width, height, has_audio,  -- filled by the probe stage
       language, created_at)           -- language: added with ASR (1.2)

jobs(id, user_id, video_id, kind,      -- 'process_video' | 'rebuild_summary' ...
     status,                           -- pending | processing | needs_review | done | failed
     stage,                            -- asr | ocr | extract | resolve | index
     progress, error, created_at, updated_at)

transcript_chunks(id, video_id, user_id, start_ms, end_ms,
                  text, ocr_text, text_unaccent,
                  dense vector(1024), sparse sparsevec)

places(id, user_id, osm_type, osm_id,  -- osm_* may be NULL when the place is not on OSM
       name, name_unaccent, lat, lng, category, area, address,
       osm_opening_hours, created_at)

place_aliases(place_id, alias, alias_unaccent)

place_mentions(id, user_id, video_id, place_id NULL,  -- one row per (place, video) pair
               payload jsonb,                          -- PlaceMention
               source_start_ms, source_end_ms, confidence, created_at)

place_match_candidates(id, mention_id, candidate_place_id, score,
                       status)          -- pending | accepted | rejected

place_cards(place_id PK, aggregated jsonb, conflicts jsonb,
            summary_text, summary_stale bool, version,
            dense vector(1024), sparse sparsevec, updated_at)

trips(id, user_id, title, start_date, end_date, area, preferences jsonb, created_at)
trip_items(id, trip_id, day, position, place_id, start_time, end_time,
           travel_from_prev jsonb,      -- {distance_m, duration_s, mode} from OSRM
           cost_estimate, note, locked bool)
```

**Indexes:**
- HNSW on `dense`.
- A `sparsevec` index on `sparse`.
- GIN `gin_trgm_ops` on `places.name_unaccent`, `place_aliases.alias_unaccent` and `transcript_chunks.text_unaccent`.
- B-tree on `user_id` in every table that holds user data.

**Security:** every read filters by `user_id`. Consider Postgres Row-Level Security as a second layer.

## 5. Feature design

### 5.1 Feature 1: Extracting places from videos

**Pipeline** (one `process_video` job, split into stages that are each idempotent and retryable):

1. **Upload:** store the file, create `video` + `job(pending)`, return `job_id`. The client opens an SSE stream at `/jobs/{id}/events`.
2. **ASR:** faster-whisper (large-v3, or turbo when speed matters), with `word_timestamps` on and automatic language detection.
3. **OCR:**
   - Frame selection:
     - Take a frame at each scene cut (PySceneDetect).
     - Also sample one frame every 1–2 seconds.
     - Drop duplicate frames using a perceptual hash (imagehash, with a configurable Hamming threshold).
   - Run PaddleOCR (supports Vietnamese) and drop low-confidence text.
4. **Chunk:**
   - Group ASR segments into chunks of about 30–60 seconds, split at sentence boundaries, with a small overlap.
   - Attach OCR text from the same time range to `ocr_text`.
5. **Extract:** the LLM reads the chunks (with timestamps) and returns a list of `PlaceMention`:

   ```python
   class PlaceMention(BaseModel):
       name: str
       category: Literal["restaurant", "cafe", "bar", "temple", "hotel",
                         "attraction", "shop", "other"]
       area: str | None              # district/ward/city mentioned
       address_hint: str | None
       price: PriceInfo | None       # min, max, currency, unit (per person / per item)
       opening_hours: str | None     # raw text as stated
       best_time: str | None         # when to go
       pros: list[str]
       cons: list[str]
       recommendation: int | None    # 1–5
       source_start_ms: int
       source_end_ms: int
       confidence: float
   ```

6. **Resolve:** for each mention:
   1. Find candidates among the user's existing `places` (trigram on name + aliases, filtered by area).
   2. Find candidates on OSM. Use Nominatim to geocode the area, then Overpass to find POIs with similar names around it.
   3. Score each candidate: `score = w1·name_sim + w2·distance_score + w3·category_match`.
      - `name_sim` is rapidfuzz on normalized names (lowercased, unaccented, generic words such as "quán" / "nhà hàng" / "restaurant" stripped).
      - `distance_score` applies only when coordinates are known.
   4. Act on the score (thresholds are configurable):

      | Score | Action |
      |---|---|
      | `≥ 0.85` | Merge automatically, add an alias if the name differs |
      | `0.60 – 0.85` | Create a `place_match_candidate`, move the job to `needs_review`, and ask the user *"Is this Shop X?"* |
      | `< 0.60` | Create a new `place` (from OSM if found; otherwise a record with just name + area that the user can pin on the map) |

7. **Aggregate:** recompute `place_cards.aggregated` with rules. **No LLM here.**

   | Field | Rule |
   |---|---|
   | Price | `[min, max]` range across all mentions, with the number of videos |
   | Opening hours | Prefer OSM `opening_hours`. Otherwise use the latest mention. If sources disagree, record it in `conflicts` with dates |
   | Best time to go | Union of values, with sources |
   | Recommendation | Mean + count |
   | Pros/cons | Kept as raw lists for the summary step |

8. **Summary:** the LLM turns pros/cons into a short description. It **only runs** when the user asks, or after a new mention arrives. In that case `summary_stale = true`, and the summary is rebuilt lazily when the card is opened or in a batch.
9. **Index:** compute dense + sparse embeddings for chunks and place cards.
10. The job moves to `done`, or to `needs_review` if candidates still need confirmation. An SSE event is sent.

**Viewing a place:** the place page shows the place card, the source videos (clicking one opens it at the right timestamp), any conflicts, and an "Ask about this place" button.

### 5.2 Feature 2: Personal trip planning

**Preference intake:**
- Required: dates (start/end) and area.
- Optional: budget, pace (tight / relaxed), priorities (photos / food / culture ...), transport mode.
- Stored in `trips.preferences`.

**Generating an itinerary:**
1. **Candidate selection:** a filtered query on the user's map (area, category, price within budget), ranked by priority score (recommendation + preference match).
2. **Travel matrix:** OSRM `table` between candidates.
3. **Solver:** OR-Tools routing with time windows:
   - Time window = opening hours.
   - Visit duration depends on the place category.
   - Each day has a start and end time.
   - Meals must fall in meal windows.
   - Buffer time depends on pace.
4. **LLM:** receives the feasible itinerary and writes notes and reasons for each stop. It must **not** change the order or the times.
5. Each `trip_item` shows: highlights, estimated cost, distance and travel time from the previous stop.

**Removing / replacing a place:**
1. The user removes an item. Items marked `locked` stay fixed.
2. Replacement query: same category, within X km of the removed place, similar price, open during that time slot, not already in the plan.
3. Check each candidate's feasibility by inserting it into the plan (OSRM + time windows).
4. The LLM picks one of the top-k feasible candidates and explains why. The user can view the alternatives.

**Adding from a video while editing a plan:**
- Reuses the 5.1 pipeline. Results go to a review screen before anything is written to the plan:
  - A mention that matches a place **already in the plan**: only its details are updated (through the place card). The schedule does not change.
  - A new mention: the user ticks the places they want to add, and the solver inserts them into the plan.

### 5.3 Feature 3: Q&A (RAG)

**Scope:** only the user's own data (videos, map, plans). Two contexts:
- **Whole map.**
- **Single place:** `place_id` is pinned as a filter.

**Two document types are indexed:**
- **Transcript chunks:** for detail questions ("Did the reviewer say anything about the seating?").
- **Place cards:** for aggregate questions ("Which places stay open late?").

**Pipeline:**
1. **Intent** (LLM, structured output): `place_lookup | filtered_search | detail_qa | plan_qa | out_of_scope`.
2. **Filters** (LLM, structured output): area, category, price range, time window, and `place_id` if a place is named.
3. **Routing by intent:**

   | Intent | Handling |
   |---|---|
   | `plan_qa` | SQL query on `trips` / `trip_items`, no RAG |
   | Distance / travel-time questions | OSRM, never the LLM |
   | `filtered_search` | Filter with SQL on `place_cards.aggregated` first (e.g. closes after 22:00), then retrieval |
   | `out_of_scope` | Say the question is outside the user's data |
   | Everything else | Retrieval (§6) |

4. **Retrieval** (§6): take the top 20, rerank down to 5–8.
5. **Context assembly:** each passage carries a source ID (`[c:123]` for chunks, `[p:45]` for place cards) and the video's date.
6. **Answer generation** with a constrained prompt:
   - Use only the provided context. Every claim must cite a source ID.
   - No data → say so directly, never guess.
   - Videos disagree → present both, with dates, without choosing.
   - Answer in the language of the question.
7. **Post-processing:** drop citations that aren't in the context. Map IDs to source cards (video + timestamp, to reopen the exact segment) and links to the place on the map.

## 6. Retrieval

```
query ──► bge-m3 ──► dense   ──► pgvector HNSW (cosine)      ─┐
                 └─► sparse  ──► pgvector sparsevec (inner)  ─┼─► RRF (k=60) ─► top 20 ─► bge-reranker-v2-m3 ─► top 5–8
query_unaccent ──────────────► pg_trgm on names/aliases/text ─┘
           (every branch: WHERE user_id = :uid AND <metadata filters>)
```

- **Dense:** captures meaning, and works across languages (an English question finds a Vietnamese transcript).
- **Sparse (bge-m3 lexical weights):** plays the role of BM25 without Vietnamese word segmentation.
- **Trigram + unaccent:** catches proper nouns typed without diacritics or misspelled ("pho ba thin", "Ginzan Onsen").
- **RRF:** combines by rank only, so scores from different branches never need normalizing.
- Retrieval sits behind a `Retriever` port. If recall turns out to be insufficient, it can be switched to ParadeDB `pg_search` or Qdrant without touching `qa`.
- **Evaluation:**
  - Build a question set (Vietnamese + English, with proper nouns and diacritic-free queries) labeled with the correct sources.
  - Measure recall@20 before rerank and nDCG@5 after rerank.
  - Rerun whenever chunking, weights or models change.

## 7. Roadmap

Each phase ends with something that runs end-to-end.

### Phase 0: Foundation ✅
Status: done. Notes from implementation:
- The test console is a single Vue 3 + Vite page (`frontend/`), meant to grow into the real UI.
- SSE auth uses a `?token=` query parameter because `EventSource` cannot send headers.
- Each SSE client holds one dedicated Postgres connection for `LISTEN`. Replace it with a shared fan-out listener if concurrent clients grow.
- Jobs are enqueued after the DB commit. If enqueueing fails, the job stays `pending` (a reaper for stale pending jobs is a later improvement).
- The procrastinate schema is installed by Alembic revision `0001`. A procrastinate upgrade that changes its schema needs a new Alembic revision with procrastinate's migration SQL.
- OSRM runs under the `geo` compose profile, because it needs preprocessed data (`scripts/osrm-prepare.sh`).

- `pyproject.toml` (uv), ruff, mypy, pytest, pre-commit, CI.
- `docker-compose`: Postgres (pgvector ≥ 0.7, pg_trgm, unaccent), api, worker, OSRM (with data for one test region).
- First Alembic migration. `core` (config, db, logging). Basic auth.
- `jobs` + procrastinate + SSE: a dummy job that goes through every state.
- **Done when:** `docker compose up` works and the dummy-job test passes.

### Phase 1: Video → place card ✅
Details, deviations and tests: [docs/phase-1-video-pipeline.md](docs/phase-1-video-pipeline.md).
- 1.1 Local upload, `VideoSource`, job creation. ✅
  - `POST /api/videos` (multipart) streams the file to storage while hashing it (SHA-256), then records the video and a `process_video` job in one commit. `GET/DELETE /api/videos[/{id}]`, `GET /api/videos/{id}/file` (Range support, for playback at a timestamp later).
  - The pipeline starts with a `probe` stage (PyAV): it rejects files that are not decodable, have no video stream, or exceed `max_video_seconds`, and stores duration/resolution/audio.
  - Storage is behind the `VideoStorage` port (`LocalVideoStorage` now, S3-compatible later). api and worker share the `media` volume.
  - Known limits: Starlette spools the multipart body to a temp file before our size check runs, so very large uploads are written twice. Enforce a body-size limit at the reverse proxy in production. Deleting a video while its job runs makes the job fail with "no longer exists".
- 1.2 ASR + timestamped chunking. ✅
- 1.3 Extraction against the `PlaceMention` schema, with versioned prompts. ✅ (Claude, structured outputs)
- 1.4 Resolution: match against existing places and OSM, confidence thresholds, an API to confirm candidates. ✅ Mentions have no coordinates, so "distance" is judged by area; the job resumes after the review.
- 1.5 Aggregation + conflicts, lazy summary. ✅ Only OSM-syntax hours are compared for conflicts.
- 1.6 OCR (frame selection + PaddleOCR) attached to chunks. ✅ Changed: periodic sampling + dHash de-duplication instead of PySceneDetect/imagehash; EasyOCR instead of PaddleOCR (shares torch with the other models).
- 1.7 Place view API: place card, source videos + timestamps. ✅
- **Done when:** uploading 3 videos about the same shop (under different names) yields 1 place with 3 mentions, and conflicts show correctly. A sample video set exists for regression tests.

### Phase 2: Q&A ✅
Details, eval results and tests: [docs/phase-2-qa.md](docs/phase-2-qa.md). Changes from the plan: intent and filters come from one LLM call; a `route` intent answers distance questions from the router.
- 2.1 Dense + sparse + trigram indexing.
- 2.2 Hybrid retriever + RRF + rerank.
- 2.3 Intent + filters, routing to SQL/OSRM.
- 2.4 Cited answer generation, post-processing, no-data and conflict handling.
- 2.5 "Ask about this place" context.
- 2.6 Retrieval evaluation set (§6).
- **Done when:** the question set meets the recall/nDCG targets set in 2.6, and questions with no data always get a "no data yet" answer.

### Phase 3: Trip planning ✅
Details and tests: [docs/phase-3-planning.md](docs/phase-3-planning.md). Restaurants get lunch/dinner variants so a day has at most one of each; replacement never moves other stops.
- 3.1 Preference intake + `trips` storage.
- 3.2 Candidate selection + OSRM matrix + OR-Tools with time windows.
- 3.3 LLM notes/reasons (must not change the schedule).
- 3.4 Remove / replace a place (filter → feasibility check → LLM pick).
- 3.5 `plan_qa` in Q&A (SQL queries).
- **Done when:** generated itineraries never violate opening hours or day time windows (covered by tests), and replacing a place never breaks `locked` items.

### Phase 4: Extensions
- 4.1 Add places from a video while editing a plan (add/update review screen).
- 4.2 `TikTokSource` (yt-dlp) via `VideoSource`.
- 4.3 Split workers by queue (`media` / `llm` / `index`), plus a model server for embedding/rerank if needed.

## 8. Open questions and risks

| # | Item | Notes |
|---|---|---|
| Q1 | ~~Which LLM provider / model?~~ | **Decided:** Claude (`claude-opus-5-5`), structured outputs, server-side refusal fallback. Tests use a scripted fake |
| Q2 | ~~Which frontend?~~ | **Decided:** Vue 3 + vue-router; map with Leaflet + OSM raster tiles now, MapLibre GL JS + OpenFreeMap before production or map layers (trip routes). See [docs/frontend-map.md](docs/frontend-map.md) |
| Q3 | ~~How does the API embed queries and rerank for live Q&A?~~ | **Decided:** our own `ml-service/` (bge-m3 dense+sparse, reranker, ASR, OCR), run natively on the host for the Apple GPU; api and worker call it over HTTP |
| Q4 | GPU infrastructure | Embeddings and rerank run on MPS (Apple GPU); faster-whisper runs on CPU on macOS. A 10 s clip takes a few seconds per stage once the models are loaded; measure on real 1–3 min videos |
| Q5 | The public Nominatim instance allows 1 request/second and forbids heavy use | Use the public instance with caching in dev. Self-host Nominatim/Overpass in production |
| Q6 | OSM is missing many small POIs (street food stalls) | Allow places without `osm_id`, pinned on the map by the user |
| Q7 | ASR quality with loud background music, regional accents, Vietnamese–English code-switching | Track with the sample video set. Consider adding VAD or vocal separation |
| Q8 | Resolution weights and thresholds | Need real data to tune. Keep them in config and log every decision for later analysis |
| R1 | LLM cost grows with the number of videos | Send only chunks likely to mention places, cache by video hash |
| R2 | Privacy (videos are personal data) | Filter by `user_id` in every query (+ RLS). Support deleting a video together with everything derived from it |
