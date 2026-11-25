# Testing

## Automated tests

```bash
docker compose up -d db                       # Postgres with pgvector (creates travelrag_test)
cd backend
uv run ruff check . && uv run ruff format --check . && uv run mypy
TRAVELRAG_TEST_DATABASE_URL=postgresql+psycopg://travelrag:travelrag@localhost:5432/travelrag_test \
  uv run pytest
```

- **No external service is called.** The LLM is scripted per task (`tests/fakes.py:
  ScriptedLLM`). ml-service is replaced by `FakeML` (hash embeddings, word-overlap reranker,
  canned ASR/OCR), Nominatim by `DemoGeocoder` (answers from the Hanoi dataset), and OSRM by
  the straight-line router. The `world` fixture installs them where the app looks them up, so
  the API and worker code paths run unchanged.
- The `seeded` fixture loads the Hanoi demo dataset (`backend/src/travelrag/demo/hanoi.py`) for
  the test user through the real resolution, aggregation and indexing code.
- Without `TRAVELRAG_TEST_DATABASE_URL`, integration tests are skipped. They apply migrations
  and truncate the job queue, so point them only at a disposable database.

| Area | Unit | Integration |
|---|---|---|
| Phase 0 (auth, jobs, SSE) | `test_job_*`, `test_sse`, `test_security` | `test_auth`, `test_jobs_flow` |
| Phase 1 (video → places) | `test_phase1`, `test_geo`, `test_media_and_storage`, `test_ingestion` | `test_videos`, `test_phase1_pipeline` |
| Phase 2 (Q&A) | `test_qa_domain` | `test_phase2_qa` |
| Phase 3 (planning) | `test_planner` | `test_phase3_trips` |

## Prompt checks with recorded responses

The tests above prove the code around the LLM, not the quality of the prompts. That is what
`backend/tests/integration/llm/` is for: it runs the production code that builds each prompt
(extraction, summary, Q&A interpretation + answer, trip notes, replacement pick), but the LLM is
a *cassette* holding **real Claude responses recorded once** in `backend/tests/llm_fixtures/`.

```bash
# replay (default; part of the normal test run and CI): no API key, no API calls
TRAVELRAG_TEST_DATABASE_URL=... uv run pytest tests/integration/llm

# record: calls Claude (~20 requests) and rewrites the fixtures. Only after changing a prompt.
TRAVELRAG_LLM_RECORD=1 TRAVELRAG_TEST_DATABASE_URL=... uv run pytest tests/integration/llm
```

- Each recorded call stores a hash of the task, system prompt, user prompt and output schema.
  If the code now builds a different prompt, replay **fails** with "re-record" instead of
  silently reusing an answer to a different question. UUIDs that change per run are normalised.
- The key is read from `TRAVELRAG_ANTHROPIC_API_KEY` or the repo's `.env`. Fixtures contain
  prompts and outputs only, and are committed so everyone replays the same responses.
- Claude is not deterministic: a recording is one sample. Re-record a few times when judging a
  prompt change, and review the fixture diff (`git diff backend/tests/llm_fixtures`).

| Case | Checks |
|---|---|
| `extract-two-shops` (real noisy ASR + OCR) | 2 places; price, hours (parsed), cons, best time; OCR address wins over ASR; "5 sao" → 5 |
| `extract-lan-can` (real TikTok video: garbled ASR, grouped OCR in `llm_fixtures/inputs/`) | the address that is only on screen ("39 Hàng Cót") lands in `address_hint` |
| `extract-review-pho` | 2 places; queue + seating cons; price; timestamps from cited chunks |
| `summary-cafe-giang` | grounded in the place's notes only |
| `ask-*` (7 questions) | intent and filters per question (late opening, cheap cafes in an area, route, out of scope, plan); both conflicting hours in the answer; citations are labels |
| `trip-notes` | one note per stop; no number that is not in the review notes |
| `replace-pick` | picks one of the given options with an LLM reason; no "famous"-style claims |

**Known prompt issues** are `xfail(strict=True)` tests. They fail on the recordings today, and
once a prompt fix makes them pass, pytest reports `XPASS(strict)` until the marker is removed:
- extraction invents a 1–5 rating when the video gives none
- the summary prompt is not given the conflicts, so it claims the videos agree

The "detail question classified as `filtered_search`" problem was seen in a manual run but not
in this recording (it is intermittent). `test_detail_questions_are_not_filtered_search` guards it.

## Manual testing with real models

### 1. Start everything

```bash
# Native model server (Apple GPU). First calls download bge-m3, the reranker (~2.3 GB each),
# faster-whisper and EasyOCR models.
cd ml-service && uv sync && uv run uvicorn ml_service.main:app --host 127.0.0.1 --port 8100

# In another terminal, from the repo root. Add your Claude key first:
echo 'TRAVELRAG_ANTHROPIC_API_KEY=sk-ant-...' >> .env
docker compose up -d --build
```

Without the key everything except extraction, summaries, notes and LLM answers still works:
the video job stops at `extract` with a clear error (Retry after adding the key), Q&A lists the
relevant passages, and trips get template notes.

### 2. Load the Hanoi demo data (no videos needed)

```bash
docker compose exec worker python -m travelrag.demo.seed \
  --email demo@example.com --password demo-pass-123 --reset
```

Log in at http://localhost:5173 with that account. The demo has 4 "videos" (no files, so they
cannot play) and 16 places.

### 3. Things to try

**Map (home page)**: see [frontend-map.md](frontend-map.md#how-to-test) for the map checks (filters, card, pinning).
- *Phở Thìn Lò Đúc* lists two videos and the alias "quán phở bò tái lăn Lò Đúc" (three names merged).
- *Cà phê Giảng* shows a conflict: 07:00–22:00 (2026-10-01) vs 08:00–23:00 (2026-11-01).
- Opening a place with an LLM key generates its summary.

**Ask page** (the example chips cover each intent)

| Question | Expect |
|---|---|
| Người review có nói gì về chỗ ngồi ở quán phở Lò Đúc không? | answer citing `review_pho_ha_noi.mp4` at 00:32 |
| Quán nào mở muộn sau 11 giờ đêm? | Bia hơi Tạ Hiện and Cộng Cà Phê only |
| Cà phê Giảng mở cửa mấy giờ? | both versions with their dates, no choice made |
| Where can I watch the sunset at a temple? | Chùa Trấn Quốc (English question, Vietnamese source) |
| Từ Văn Miếu tới phở Thìn bao xa? | km + minutes, marked as an estimate without OSRM |
| Thủ đô của Pháp là gì? | out of scope |
| "Ask about this place" on a place card (Map) | answers only from that place; the places in an answer link back to the map |

**Trips page**
- Build a 2-day trip starting on a Monday: Hoàng thành Thăng Long only appears on day 2, and
  each day has at most one lunch and one dinner.
- Replace a restaurant: you get a nearby restaurant that fits the same slot, and the other
  stops keep their times. Click an alternative to use it instead.
- Lock a stop: Replace and Remove are refused for it.
- Ask "Ngày 2 đi đâu?" on the Ask tab: the answer is read from the trip.

**Videos page (full pipeline)**
- *LLM result* on a video opens what the LLM concluded: one card per place with every field it
  filled ("not stated" for the rest), where the place went on the map, and, under *What the LLM
  read*, the transcript and on-screen text it was given. Use it to see why a field is wrong.
- Upload a short travel video with speech. The job goes through probe → asr → ocr → extract →
  resolve → aggregate → index. Uncertain matches appear on the Map page under *places to confirm*, and the job waits in `needs_review` until you answer.
- To test without filming, synthesise one (macOS voice + a frame with the shop name):
  ```bash
  cd backend && uv run python ../scripts/make_test_video.py /tmp/pho.mp4 \
    --say "Hôm nay mình ăn phở Thìn Lò Đúc, giá sáu mươi lăm nghìn một tô, mở cửa từ sáu giờ sáng." \
    --overlay "PHỞ THÌN" --overlay "13 LÒ ĐÚC"
  ```
  The transcript (*Videos → job → `GET /api/videos/{id}/transcript`*) shows what ASR heard;
  the OCR text usually corrects the shop name.

### 4. Retrieval evaluation

```bash
docker compose exec worker python -m travelrag.qa.eval --email demo@example.com
```

It prints recall@20 and nDCG@5 per question and their means (0.97 / 0.84 on 2026-11-23).

### 5. Real travel times (optional)

```bash
./scripts/osrm-prepare.sh          # Vietnam extract
docker compose --profile geo up -d osrm
echo 'TRAVELRAG_OSRM_URL=http://osrm:5000' >> .env && docker compose up -d api worker
```

New trips then report `routing_source: osrm`.
