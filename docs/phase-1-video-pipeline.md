# Phase 1 — Video → places

How an uploaded video becomes places on the user's map. Phase 2 (Q&A) and Phase 3 (trip
planning) both read what this phase produces, so it is documented here too.

## Overview

```
upload ──► process_video job (worker)
            probe ─► asr ─► ocr + chunking ─► extract ─► resolve ─► aggregate ─► index
                                                           │
                                       uncertain match ────┘──► needs_review ─► user confirms
                                                                                 │
                                                       job re-enqueued, continues ◄┘
```

| Stage | Module | What it does | Output |
|---|---|---|---|
| `probe` | `media` (PyAV) | Rejects unreadable / no-video / too-long files | `videos.duration_ms, width, height, has_audio` |
| `asr` | `ml-service` (faster-whisper) | Audio → 16 kHz mono WAV → timestamped segments | `videos.transcript`, `videos.language` |
| `ocr` | `media` + `ml-service` (EasyOCR) | Samples frames, drops near-duplicates, reads on-screen text | `videos.ocr` |
| chunking | `media.chunking` | Groups segments into 30–60 s chunks; attaches OCR text | `transcript_chunks` |
| `extract` | `extraction` (Claude) | Chunks → `PlaceMention` list (fixed schema) | `place_mentions` |
| `resolve` | `places` (+ OSM) | Mention → existing place / OSM POI / new place, or ask the user | `places`, `place_aliases`, `place_match_candidates` |
| `aggregate` | `places` | Merges all mentions of a place with fixed rules | `place_cards.aggregated`, `conflicts` |
| `index` | `indexing` (bge-m3) | Dense + sparse embeddings for chunks and cards | `*.dense`, `*.sparse` |

**Every stage can be skipped when it has already run** (`duration_ms`, `transcript`, `ocr`,
chunks and `extracted_at` are checked first). A retried job, or one resumed after a review,
continues where it stopped. Neither ASR nor the LLM runs twice for the same video.

## Stages in detail

### ASR and OCR
- PyAV decodes the audio track and resamples it to 16 kHz mono PCM, which is sent to
  `ml-service /transcribe` (faster-whisper with VAD and automatic language detection).
- **Frame selection** takes one frame every `TRAVELRAG_OCR_SAMPLE_SECONDS` (1.5 s). It keeps a
  frame only when its 64-bit difference hash (dHash) differs from the last kept frame by more
  than 10 bits. That single pass catches scene cuts and drops near-identical frames, so
  PySceneDetect / imagehash are not needed (a change from PLAN.md). At most 120 frames per video.
- EasyOCR returns one box per word group, often splitting a line ("39", "Hàng", "Cót") and in
  no reliable order. ml-service (`text_lines.py`) joins boxes that share a row (vertical overlap
  ≥ 50 % with the row's first box, horizontal gap ≤ 1.5 line heights) left to right, and orders
  lines top to bottom, so an overlay reads "LAN CAN | 39 Hàng Cót". A line's confidence is the
  mean of its words, weighted by length.
- OCR lines with confidence < 0.4 or shorter than 2 characters are discarded.
- On-screen text often fixes ASR mistakes. In the end-to-end test with a synthesised voice, ASR
  heard "phở *thịn*" while OCR read "PHỞ THÌN | 13 LÒ ĐÚC".

### Chunking (`media/chunking.py`)
- Segments are grouped until a chunk lasts ≥ 30 s and ends on a sentence, or reaches 60 s.
- The boundary segment is repeated at the start of the next chunk (overlap), so a sentence split
  across chunks is still retrievable as a whole.
- OCR windows do **not** overlap: chunk *i* gets the frames shown between the end of chunk *i−1*
  and its own end.
- A video without speech gets OCR-only chunks of 30 s.

### Extraction (`extraction/`)
- One structured-output call to Claude (`claude-opus-5-5`, effort `medium`) per video, with the
  numbered chunks (timestamps + on-screen text) as input. The schema is `ExtractedMention`:
  name, category, area, address hint, price min/max/currency/unit, opening hours (raw text and
  OSM syntax), best time, pros, cons, recommendation 1–5, chunk numbers, confidence.
- Post-processing in code, not trusted from the model:
  - source timestamps come from the cited chunk numbers (an invented chunk number falls back
    to the whole video)
  - `opening_hours_osm` that does not parse is dropped (the raw text is kept)
  - recommendation clamped to 1–5, negative prices dropped, empty pros/cons removed
  - mentions with confidence < 0.3 are skipped
- Server-side refusal fallback is enabled (`fallbacks: "default"`).
- Without `TRAVELRAG_ANTHROPIC_API_KEY` the job fails at this stage with a clear message.
  After adding the key, **Retry** continues from `extract`.

### Resolution (`places/service.py`)
For each mention:
1. **Existing places**: trigram search on the normalised name (`name_norm`) and aliases.
2. **OSM**: geocode the mention's area with Nominatim, then search the name inside that
   bounding box (Overpass as fallback). POIs already on the user's map (same OSM id) count as
   existing places.
   - Only results of category `boundary` or `place` count as an area. Free-text search also
     ranks POIs: "Phố cổ, Hà Nội" returns the restaurant "Phở Cồ" first, whose tiny box used to
     hide every other place.
   - If the area is unknown or not found, `TRAVELRAG_DEFAULT_REGION` is searched instead, never
     the whole world. OSM hits are then not counted as agreeing on area.
3. **Score** = `0.6 · name + 0.25 · area + 0.15 · category`
   - name: max(token-set ratio, ratio) on unaccented names with generic words removed
     ("quán", "nhà hàng", "cà phê"…)
   - area: 1 if the areas match, 0 if they differ, 0.5 if unknown (mentions carry no
     coordinates, so the area stands in for "distance"; OSM hits found inside the mentioned
     area's bounding box count as matching)
   - category: 1 / 0, or 0.5 if unknown
4. **Decide** (thresholds in config):

   | Best score | Action |
   |---|---|
   | ≥ 0.85 (`RESOLVE_AUTO_MERGE`) | attach; the mention's name becomes an alias |
   | 0.60 – 0.85 (`RESOLVE_ASK_USER`) | up to 3 candidates; mention → `review`; job → `needs_review` |
   | < 0.60 | new place (from the OSM POI if there was one, otherwise name + area only) |

5. **Address pin**: a place that still has no coordinates (not on OSM) but whose mention has an
   `address_hint` is geocoded by address inside the same box. Only street-sized results (span
   ≤ 0.015°, about 1.5 km) are accepted. House numbers are rarely mapped in Vietnam, so the pin
   usually sits on the right street rather than the exact door, and the place card says so. A
   ward- or city-level answer is rejected, because its centre would look precise and be wrong.
   The extraction prompt (`extract-v2`) asks for the on-screen address explicitly, since
   videos often show it in an overlay and never say it.

**Review**: `GET /api/reviews` lists the pending questions ("Is this *X*?"). `POST
/api/reviews/candidates/{id}/accept` attaches the mention, and `POST
/api/reviews/mentions/{id}/reject` creates a new place from it. When no mention of the video is
left in review, the job is re-enqueued and finishes the remaining stages. Places unknown to OSM
can be pinned with `PATCH /api/places/{id}/location`.

### Aggregation (`places/domain.py: aggregate`)
Each `(place, video)` pair keeps its own mention. The card is recomputed **with rules, not the
LLM**:

| Field | Rule |
|---|---|
| price | overall `[min, max]` across mentions + number of videos; disjoint ranges → conflict |
| opening hours | OSM wins; otherwise the newest video. Only machine-comparable values (OSM syntax) can conflict; free text such as "mở tới khuya" is shown but never compared |
| best time / pros / cons | union, de-duplicated case-insensitively, each with its video and date |
| recommendation | mean + count |

Conflicts keep every version with its video name and date. Q&A shows them as they are and
never picks one.

**Summary**: the LLM turns pros/cons/best time into 2–4 sentences. It runs only when the card
changed (`summary_stale`) and the user opens the place (`GET /api/places/{id}`), or on
`POST /api/places/{id}/summary`. Without an LLM key the card simply has no summary.

### Indexing (`indexing/service.py`)
- Chunks: bge-m3 dense (1024-d, cosine) + sparse (lexical weights) of `text + OCR text`.
- Cards: the same for `card_text` (name, aliases, address, price, hours, pros/cons, summary,
  conflicts). Cards carry `version` / `indexed_version`, so only changed cards are re-embedded.
- `search_text` (unaccented) on both feeds the trigram branch of retrieval.

### Deleting a video
`DELETE /api/videos/{id}` removes the file, jobs, transcript chunks and mentions, then updates
every place the video mentioned (`PlacesService.refresh_after_removal`):
- **no other video mentions it**: the place is deleted, together with its card, aliases,
  review candidates and any trip stops at it
- **other videos still mention it**: aliases only the deleted video used are dropped, the LLM
  summary is cleared (it may quote the deleted video and is regenerated lazily), and the card
  is recomputed from the remaining mentions. Price, hours, pros/cons and conflicts then follow,
  and the card is re-embedded (best effort, if ml-service is up)
- a mention still waiting for review whose candidate places were all deleted becomes a new
  place, and its job continues

Existing trips lose stops at deleted places. Their remaining stops and notes are not
re-planned: build a new trip to use the updated cards.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/videos/{id}/transcript` | chunks with timestamps and OCR text |
| GET | `/api/videos/{id}/extraction` | what the LLM concluded: each extracted place with every field (null = not stated), confidence, time range, and its status (attached to which place / waiting for review) |
| GET | `/api/places` | the map: one entry per place (price, rating, #videos, conflict flag) |
| GET | `/api/places/{id}` | card, conflicts, summary (lazily generated), mentions with video + timestamp |
| POST | `/api/places/{id}/summary` | regenerate the summary |
| PATCH | `/api/places/{id}/location` | pin a place OSM did not know |
| GET | `/api/reviews` | mentions waiting for confirmation, with candidates |
| POST | `/api/reviews/candidates/{id}/accept` | "yes, it's this one" |
| POST | `/api/reviews/mentions/{id}/reject` | "none of these": new place |

## How to test

Automated (fake models, real Postgres):

| File | Covers |
|---|---|
| `tests/unit/test_phase1.py` | chunk sizes, overlap and OCR windows; extraction cleanup and timestamps; match scoring; aggregation rules and conflicts; stage order; review → resume without re-running ASR or the LLM |
| `tests/unit/test_media_and_storage.py` | probing real generated videos |
| `tests/unit/test_geo.py` | Vietnamese normalisation, opening-hours parser, straight-line router |
| `tests/unit/test_osm_geocoder.py` | on recorded Nominatim answers: area boxes skip POIs ("Phở Cồ"); address → street-level point; ward-level answer rejected |
| `ml-service/tests/test_text_lines.py` | OCR word boxes → lines in reading order, no chaining across lines (`cd ml-service && uv run --with pytest python -m pytest tests`) |
| `tests/integration/test_video_deletion.py` | deleting a video removes places only it mentioned, recomputes shared cards (price, cons, aliases, conflicts, summary, embedding), drops its stops from trips, keeps it out of Q&A, and settles reviews left without candidates |
| `tests/integration/test_phase1_pipeline.py` | upload → worker → places + cards + transcript via the API; `needs_review` → accept → job resumes → done; reject → new place; pin location; a place unknown to OSM is pinned from its on-screen address; demo seed merges three names of one pho shop and flags exactly one conflict |

Manual: see [testing.md](testing.md).
