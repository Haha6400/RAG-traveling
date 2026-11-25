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

**Places tab**
- *Phở Thìn Lò Đúc* lists two videos and the alias "quán phở bò tái lăn Lò Đúc" (three names merged).
- *Cà phê Giảng* shows a conflict: 07:00–22:00 (2026-10-01) vs 08:00–23:00 (2026-11-01).
- Opening a place with an LLM key generates its summary.

**Ask tab** (the example chips cover each intent)

| Question | Expect |
|---|---|
| Người review có nói gì về chỗ ngồi ở quán phở Lò Đúc không? | answer citing `review_pho_ha_noi.mp4` at 00:32 |
| Quán nào mở muộn sau 11 giờ đêm? | Bia hơi Tạ Hiện and Cộng Cà Phê only |
| Cà phê Giảng mở cửa mấy giờ? | both versions with their dates, no choice made |
| Where can I watch the sunset at a temple? | Chùa Trấn Quốc (English question, Vietnamese source) |
| Từ Văn Miếu tới phở Thìn bao xa? | km + minutes, marked as an estimate without OSRM |
| Thủ đô của Pháp là gì? | out of scope |
| "Ask about this place" on a place card | answers only from that place |

**Trips tab**
- Build a 2-day trip starting on a Monday: Hoàng thành Thăng Long only appears on day 2, and
  each day has at most one lunch and one dinner.
- Replace a restaurant: you get a nearby restaurant that fits the same slot, and the other
  stops keep their times. Click an alternative to use it instead.
- Lock a stop: Replace and Remove are refused for it.
- Ask "Ngày 2 đi đâu?" on the Ask tab: the answer is read from the trip.

**Videos tab (full pipeline)**
- Upload a short travel video with speech. The job goes through probe → asr → ocr → extract →
  resolve → aggregate → index. Uncertain matches appear in *Places → Needs your
  confirmation*, and the job waits in `needs_review` until you answer.
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
