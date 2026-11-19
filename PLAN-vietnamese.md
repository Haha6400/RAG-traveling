# PLAN — RAG-traveling

Tài liệu thiết kế và lộ trình. README.md là phần giới thiệu ngắn. File này là nguồn tham chiếu chính khi triển khai.

## Mục lục
1. [Mục tiêu và phạm vi](#1-mục-tiêu-và-phạm-vi)
2. [Các quyết định đã chốt](#2-các-quyết-định-đã-chốt)
3. [Kiến trúc](#3-kiến-trúc)
4. [Data model](#4-data-model)
5. [Thiết kế từng chức năng](#5-thiết-kế-từng-chức-năng)
6. [Retrieval](#6-retrieval)
7. [Lộ trình](#7-lộ-trình)
8. [Câu hỏi mở và rủi ro](#8-câu-hỏi-mở-và-rủi-ro)

---

## 1. Mục tiêu và phạm vi

Ba chức năng chính, cùng chạy trên một lõi dữ liệu là **bản đồ cá nhân** của mỗi người dùng:

```
Video ──► Transcript + OCR ──► PlaceMention (place × video) ──► Place (OSM id)
                                                                   │
                              ┌────────────────────────────────────┼──────────────────┐
                              ▼                                    ▼                  ▼
                     Place card (tổng hợp)              Lịch trình (ràng buộc)    Hỏi đáp RAG
```

**Nguyên tắc xuyên suốt:** LLM chỉ làm việc liên quan đến ngôn ngữ. Mọi thứ cần đúng tuyệt đối thì làm bằng code.

| LLM làm | Code làm (deterministic) |
|---|---|
| Trích xuất theo schema, tổng hợp mô tả, phân loại intent, trích bộ lọc, chọn và giải thích gợi ý | Gộp địa điểm, gộp số liệu, khoảng cách và thời gian di chuyển, giờ mở cửa, xếp lịch, truy vấn plan |

**Ngoài phạm vi MVP:** tải video từ link TikTok hoặc link online khác (sẽ làm sau qua adapter `VideoSource`), chia sẻ map giữa nhiều người dùng, mobile app.

## 2. Các quyết định đã chốt

| # | Quyết định | Lý do |
|---|---|---|
| D1 | Toàn bộ backend viết bằng **Python** | Phần ML (ASR, OCR, embedding, rerank) có hệ sinh thái tốt nhất trên Python, nên không cần sidecar |
| D2 | **Modular monolith**, một codebase chạy hai tiến trình `api` và `worker` | Đơn giản cho MVP. Sau này tách worker theo queue chỉ cần đổi cấu hình triển khai |
| D3 | Bên trong mỗi module tổ chức theo **ports & adapters** | Test được logic gộp, ngưỡng tin cậy và solver mà không cần gọi OSM/LLM thật. Dễ đổi provider |
| D4 | Xử lý video **bất đồng bộ** qua job queue, có trạng thái và thông báo | Xử lý một video mất từ vài chục giây tới vài phút |
| D5 | Giai đoạn đầu **chỉ nhận upload từ local**, TikTok làm sau | Giảm rủi ro (yt-dlp dễ hỏng, ToS) |
| D6 | ASR dùng **faster-whisper**. OCR dùng frame theo đổi cảnh + lấy mẫu định kỳ, bỏ frame trùng | Phần lớn video không có phụ đề sẵn. Chữ trên màn hình chứa tên quán, giá |
| D7 | Gộp địa điểm theo **ngưỡng tin cậy**: tự gộp / hỏi người dùng / tạo mới | Tránh gộp nhầm hai quán khác nhau |
| D8 | Dữ liệu địa lý dùng **OSM**: Nominatim + Overpass, OSRM | Không bị ràng buộc ToS của Google Places về việc lưu trữ dữ liệu |
| D9 | Retrieval dùng **bge-m3 dense + bge-m3 sparse + pg_trgm**, gộp bằng RRF, rồi rerank | Hỗ trợ đa ngôn ngữ Việt/Anh mà không cần tách từ tiếng Việt. pg_trgm + unaccent bắt được tên riêng gõ không dấu hoặc gõ sai. Chỉ cần một DB |
| D10 | Queue dùng **procrastinate** (chạy trên Postgres) | Không cần thêm Redis. Job và dữ liệu nằm cùng transaction |

## 3. Kiến trúc

### 3.1 Tiến trình

```
┌──────────────┐   enqueue    ┌───────────────────────────┐
│  api         │ ───────────► │  Postgres                 │
│  (FastAPI)   │ ◄─ NOTIFY ── │  - dữ liệu                │
│  SSE events  │              │  - procrastinate queue    │
└──────────────┘              │  - pgvector / pg_trgm     │
       ▲                      └───────────────────────────┘
       │ Q&A cần embedding/rerank        ▲
       │ (load model nhẹ hoặc gọi         │ đọc job, ghi kết quả
       │  model server, xem §8)           │
                              ┌───────────────────────────┐
                              │  worker                   │
                              │  faster-whisper, PaddleOCR│
                              │  bge-m3, reranker, LLM    │
                              └───────────────────────────┘
                                         │
                              Nominatim / Overpass / OSRM
```

### 3.2 Module

| Module | Trách nhiệm |
|---|---|
| `ingestion` | Nhận upload, lưu file, tạo `video` + `job`. Interface `VideoSource` (hiện có `LocalUpload`, sau này thêm `TikTokSource`) |
| `media` | ASR, chọn frame, OCR, chia chunk theo timestamp |
| `extraction` | Gọi LLM để trích xuất ra danh sách `PlaceMention` (schema Pydantic) |
| `places` | Resolution (so khớp với OSM và với địa điểm đã có), aggregation, summary, tạo place card |
| `geo` | Client cho Nominatim, Overpass, OSRM, parser `opening_hours` |
| `planning` | Thu thập mong muốn, chọn ứng viên, solver, thay thế địa điểm, cập nhật lịch trình từ video |
| `qa` | Intent → bộ lọc → retrieval → rerank → sinh câu trả lời |
| `indexing` | Sinh dense + sparse embedding, cập nhật index khi chunk hoặc place card thay đổi |
| `llm` | Client chung, structured output, lưu prompt có version |
| `jobs` | Máy trạng thái của job, phát sự kiện |
| `core` | Config, DB session, auth, logging |

### 3.3 Cấu trúc bên trong một module

```
places/
├── domain.py      # entity + rule thuần Python (Place, PlaceMention, AggregationPolicy)
├── service.py     # use case: resolve_mention(), rebuild_card()
├── ports.py       # typing.Protocol: GeoProvider, Embedder, LLM, PlaceRepository
└── adapters/      # osm.py, pg_repo.py ...
```

**Quy tắc phụ thuộc:**
- `domain` không import gì ngoài stdlib/pydantic.
- `service` chỉ phụ thuộc `domain` + `ports`.
- `adapters` implement `ports`.
- Module này chỉ gọi module khác qua `service` của module đó, không import `adapters` của nhau.

### 3.4 Sự kiện giữa các module

| Sự kiện | Phát ra từ | Phản ứng |
|---|---|---|
| `VideoUploaded` | ingestion | enqueue pipeline xử lý video |
| `MentionsExtracted` | extraction | resolution |
| `PlaceUpdated` | places | chạy lại aggregation, re-index place card, đánh dấu summary cần tạo lại |
| `MatchNeedsReview` | places | job chuyển sang `needs_review`, thông báo người dùng |

## 4. Data model

```sql
users(id, email, created_at)

videos(id, user_id, source,            -- 'local' | 'tiktok' (sau)
       file_path, original_name, duration_ms, language, created_at)

jobs(id, user_id, video_id, kind,      -- 'process_video' | 'rebuild_summary' ...
     status,                           -- pending | processing | needs_review | done | failed
     stage,                            -- asr | ocr | extract | resolve | index
     progress, error, created_at, updated_at)

transcript_chunks(id, video_id, user_id, start_ms, end_ms,
                  text, ocr_text, text_unaccent,
                  dense vector(1024), sparse sparsevec)

places(id, user_id, osm_type, osm_id,  -- osm_* có thể NULL nếu không tìm thấy trên OSM
       name, name_unaccent, lat, lng, category, area, address,
       osm_opening_hours, created_at)

place_aliases(place_id, alias, alias_unaccent)

place_mentions(id, user_id, video_id, place_id NULL,  -- một bản ghi cho mỗi cặp (place, video)
               payload jsonb,                          -- PlaceMention
               source_start_ms, source_end_ms, confidence, created_at)

place_match_candidates(id, mention_id, candidate_place_id, score,
                       status)          -- pending | accepted | rejected

place_cards(place_id PK, aggregated jsonb, conflicts jsonb,
            summary_text, summary_stale bool, version,
            dense vector(1024), sparse sparsevec, updated_at)

trips(id, user_id, title, start_date, end_date, area, preferences jsonb, created_at)
trip_items(id, trip_id, day, position, place_id, start_time, end_time,
           travel_from_prev jsonb,      -- {distance_m, duration_s, mode} lấy từ OSRM
           cost_estimate, note, locked bool)
```

**Index:**
- HNSW cho `dense`.
- `sparsevec` index cho `sparse`.
- GIN `gin_trgm_ops` trên `places.name_unaccent`, `place_aliases.alias_unaccent`, `transcript_chunks.text_unaccent`.
- B-tree trên `user_id` ở mọi bảng có dữ liệu người dùng.

**Bảo mật:** mọi truy vấn đọc dữ liệu đều lọc `user_id`. Cân nhắc bật Postgres Row-Level Security để có thêm một lớp chặn.

## 5. Thiết kế từng chức năng

### 5.1 Chức năng 1: Trích xuất thông tin từ video

**Pipeline** (một job `process_video`, chia thành từng stage, mỗi stage idempotent và có thể retry):

1. **Upload:** lưu file, tạo `video` + `job(pending)`, trả về `job_id`. Client mở SSE `/jobs/{id}/events`.
2. **ASR:** faster-whisper (large-v3, hoặc turbo nếu cần nhanh), bật `word_timestamps`, tự nhận diện ngôn ngữ.
3. **OCR:**
   - Chọn frame:
     - Lấy frame tại các điểm đổi cảnh (PySceneDetect).
     - Lấy thêm mẫu mỗi 1–2 giây.
     - Bỏ frame trùng bằng perceptual hash (imagehash, ngưỡng Hamming cấu hình được).
   - Chạy PaddleOCR (hỗ trợ tiếng Việt) và lọc text có độ tin cậy thấp.
4. **Chunk:**
   - Ghép các segment ASR thành chunk khoảng 30–60 giây, cắt ở ranh giới câu, có overlap nhỏ.
   - Gắn text OCR có cùng khoảng thời gian vào `ocr_text`.
5. **Extract:** LLM nhận các chunk (kèm timestamp) và trả về danh sách `PlaceMention`:

   ```python
   class PlaceMention(BaseModel):
       name: str
       category: Literal["restaurant", "cafe", "bar", "temple", "hotel",
                         "attraction", "shop", "other"]
       area: str | None              # quận/phường/thành phố được nhắc tới
       address_hint: str | None
       price: PriceInfo | None       # min, max, currency, unit (per person / per item)
       opening_hours: str | None     # văn bản nguyên gốc
       best_time: str | None         # thời điểm nên đi
       pros: list[str]
       cons: list[str]
       recommendation: int | None    # 1–5
       source_start_ms: int
       source_end_ms: int
       confidence: float
   ```

6. **Resolve:** với mỗi mention:
   1. Tìm ứng viên trong các `places` sẵn có của người dùng (trigram trên tên + alias, lọc theo khu vực).
   2. Tìm ứng viên trên OSM. Dùng Nominatim để geocode khu vực, rồi dùng Overpass để tìm POI có tên tương tự quanh khu vực đó.
   3. Chấm điểm: `score = w1·name_sim + w2·distance_score + w3·category_match`.
      - `name_sim` tính bằng rapidfuzz trên tên đã chuẩn hóa (lowercase, unaccent, bỏ các từ như "quán", "nhà hàng").
      - `distance_score` chỉ có khi đã có tọa độ.
   4. Quyết định theo điểm (các ngưỡng cấu hình được):

      | Điểm | Hành động |
      |---|---|
      | `≥ 0.85` | Gộp tự động, thêm alias nếu tên khác |
      | `0.60 – 0.85` | Tạo `place_match_candidate`, job chuyển `needs_review`, hỏi người dùng *"Đây có phải Quán X không?"* |
      | `< 0.60` | Tạo `place` mới (từ OSM nếu có, nếu không thì tạo bản ghi chỉ có tên + khu vực, chờ người dùng ghim lên map) |

7. **Aggregate:** tính lại `place_cards.aggregated` bằng quy tắc, **không dùng LLM**:

   | Trường | Quy tắc |
   |---|---|
   | Giá | Khoảng `[min, max]` trên tất cả mention. Ghi chú số video |
   | Giờ mở cửa | Ưu tiên `opening_hours` của OSM. Nếu không có thì lấy mention mới nhất. Nếu các nguồn khác nhau thì ghi vào `conflicts` kèm ngày |
   | Thời điểm nên đi | Hợp các giá trị, kèm nguồn |
   | Mức khuyên đi | Trung bình + số lượng |
   | Ưu/nhược điểm | Giữ danh sách thô, dùng cho bước summary |

8. **Summary:** LLM tổng hợp phần mô tả (pros/cons → đoạn văn). **Chỉ chạy** khi người dùng yêu cầu hoặc khi có mention mới (lúc đó `summary_stale = true`, chạy lười lúc người dùng mở thẻ, hoặc chạy theo lô).
9. **Index:** sinh dense + sparse cho chunk và place card.
10. Job chuyển `done`, hoặc `needs_review` nếu còn candidate chờ xác nhận. Phát sự kiện qua SSE.

**Xem lại thông tin:** trang địa điểm hiển thị place card, danh sách video nguồn (bấm vào để mở đúng timestamp), các mâu thuẫn, và nút "Hỏi về địa điểm này".

### 5.2 Chức năng 2: Lập lịch trình cá nhân

**Thu thập mong muốn:**
- Bắt buộc: thời gian (ngày đi/về), khu vực.
- Tùy chọn: ngân sách, nhịp độ (chặt/thong thả), ưu tiên (chụp ảnh / ăn uống / văn hóa...), phương tiện di chuyển.
- Lưu ở `trips.preferences`.

**Sinh lịch trình:**
1. **Chọn ứng viên:** truy vấn có bộ lọc trên map của người dùng (khu vực, loại, giá phù hợp ngân sách), sắp xếp theo điểm ưu tiên (mức khuyên đi + mức khớp sở thích).
2. **Ma trận di chuyển:** gọi OSRM `table` giữa các ứng viên.
3. **Solver:** OR-Tools routing với ràng buộc khung giờ:
   - Khung giờ = giờ mở cửa.
   - Thời gian ở mỗi điểm phụ thuộc loại địa điểm.
   - Mỗi ngày có giờ bắt đầu và kết thúc.
   - Bữa ăn phải rơi vào khung giờ ăn.
   - Thời gian nghỉ phụ thuộc nhịp độ.
4. **LLM:** nhận lịch trình đã khả thi, viết ghi chú và lý do cho từng điểm. **Không** được thay đổi thứ tự hay giờ.
5. Mỗi `trip_item` hiển thị: đặc điểm, chi phí ước tính, khoảng cách và thời gian từ điểm trước.

**Xóa / thay địa điểm:**
1. Người dùng xóa một item. Các item `locked` được giữ nguyên.
2. Truy vấn thay thế: cùng loại, trong bán kính X km quanh vị trí cũ, giá tương đương, mở cửa trong khung giờ đó, chưa có trong lịch trình.
3. Kiểm tra từng ứng viên có khả thi không bằng cách chèn vào lịch trình (OSRM + khung giờ).
4. LLM chọn một trong top-k ứng viên khả thi và giải thích lý do. Người dùng có thể xem các lựa chọn khác.

**Thêm từ video trong màn chỉnh sửa lịch trình:**
- Dùng lại pipeline 5.1. Kết quả hiện ra màn review trước khi ghi vào lịch trình:
  - Mention khớp với địa điểm **đã có trong lịch trình**: chỉ cập nhật đặc điểm (qua place card), không đổi lịch.
  - Mention mới: người dùng tick chọn những địa điểm muốn thêm, solver chèn vào lịch trình.

### 5.3 Chức năng 3: Hỏi đáp (RAG)

**Phạm vi:** chỉ dùng dữ liệu của người dùng (video, map, plan). Có hai ngữ cảnh:
- **Toàn bộ map.**
- **Một địa điểm:** cố định `place_id` làm bộ lọc.

**Hai loại tài liệu được index:**
- **Transcript chunk:** dùng cho câu hỏi chi tiết ("Người review có nói gì về chỗ ngồi không?").
- **Place card:** dùng cho câu hỏi tổng hợp ("Quán nào mở muộn?").

**Pipeline:**
1. **Intent** (LLM, structured output): `place_lookup | filtered_search | detail_qa | plan_qa | out_of_scope`.
2. **Bộ lọc** (LLM, structured output): khu vực, loại, khoảng giá, khung giờ, `place_id` nếu nhắc tên.
3. **Định tuyến theo intent:**

   | Intent | Cách xử lý |
   |---|---|
   | `plan_qa` | Truy vấn SQL trên `trips` / `trip_items`, không dùng RAG |
   | Câu hỏi về khoảng cách / thời gian di chuyển | OSRM, không lấy từ LLM |
   | `filtered_search` | Ưu tiên lọc SQL trên `place_cards.aggregated` (ví dụ giờ đóng cửa > 22:00), sau đó mới tới retrieval |
   | `out_of_scope` | Trả lời rằng câu hỏi nằm ngoài dữ liệu của người dùng |
   | Còn lại | Retrieval (§6) |

4. **Retrieval** (§6): lấy top 20, rerank còn 5–8.
5. **Ghép ngữ cảnh:** mỗi đoạn kèm ID nguồn (`[c:123]` cho chunk, `[p:45]` cho place card), ngày của video.
6. **Sinh câu trả lời** với prompt có ràng buộc:
   - Chỉ dùng thông tin trong ngữ cảnh. Mỗi ý phải trích dẫn ID nguồn.
   - Không có dữ liệu → nói thẳng là chưa có, không suy đoán.
   - Thông tin mâu thuẫn giữa các video → nêu cả hai, kèm ngày, không tự chọn.
   - Trả lời bằng ngôn ngữ của câu hỏi.
7. **Hậu kiểm:** bỏ các trích dẫn không có trong ngữ cảnh. Map ID sang thẻ nguồn (video + timestamp để mở đúng đoạn) và link tới địa điểm trên map.

## 6. Retrieval

```
query ──► bge-m3 ──► dense   ──► pgvector HNSW (cosine)      ─┐
                 └─► sparse  ──► pgvector sparsevec (inner)  ─┼─► RRF (k=60) ─► top 20 ─► bge-reranker-v2-m3 ─► top 5–8
query_unaccent ──────────────► pg_trgm trên tên/alias/text   ─┘
           (mọi nhánh: WHERE user_id = :uid AND <metadata filter>)
```

- **Dense:** bắt nghĩa, hoạt động được giữa hai ngôn ngữ (hỏi bằng tiếng Anh vẫn tìm được transcript tiếng Việt).
- **Sparse (bge-m3 lexical weights):** đóng vai trò của BM25 mà không cần tách từ tiếng Việt.
- **Trigram + unaccent:** bắt tên riêng gõ không dấu hoặc gõ sai ("pho ba thin", "Ginzan Onsen").
- **RRF:** chỉ cộng theo thứ hạng, không cần chuẩn hóa điểm giữa các nhánh.
- Retrieval đặt sau port `Retriever`. Nếu sau này đo thấy chưa đủ recall thì có thể đổi sang ParadeDB `pg_search` hoặc Qdrant mà không ảnh hưởng `qa`.
- **Đánh giá:**
  - Tạo bộ câu hỏi mẫu (Việt + Anh, có tên riêng, có câu không dấu) kèm nguồn đúng.
  - Đo recall@20 trước rerank và nDCG@5 sau rerank.
  - Chạy lại mỗi khi đổi chunking, trọng số hoặc model.

## 7. Lộ trình

Mỗi phase kết thúc bằng một bản chạy được end-to-end.

### Phase 0: Nền tảng
- `pyproject.toml` (uv), ruff, mypy, pytest, pre-commit, CI.
- `docker-compose`: Postgres (pgvector ≥ 0.7, pg_trgm, unaccent), api, worker, OSRM (dữ liệu một vùng thử nghiệm).
- Alembic migration đầu tiên. `core` (config, db, logging). Auth đơn giản.
- `jobs` + procrastinate + SSE: một job giả chạy qua đủ các trạng thái.
- **Xong khi:** `docker compose up` chạy được, test của job giả pass.

### Phase 1: Video → Place card
- 1.1 Upload local, `VideoSource`, tạo job.
- 1.2 ASR + chunk theo timestamp.
- 1.3 Extraction theo schema `PlaceMention` + prompt có version.
- 1.4 Resolution: so với địa điểm đã có, so với OSM, ngưỡng tin cậy, API xác nhận candidate.
- 1.5 Aggregation + conflicts, summary chạy lười.
- 1.6 OCR (chọn frame + PaddleOCR) gắn vào chunk.
- 1.7 API xem địa điểm: place card, nguồn video + timestamp.
- **Xong khi:** upload 3 video có cùng một quán (gọi bằng tên khác nhau) cho ra 1 place với 3 mention, các mâu thuẫn hiển thị đúng. Có bộ video mẫu để test hồi quy.

### Phase 2: Hỏi đáp
- 2.1 Indexing dense + sparse + trigram.
- 2.2 Retriever hybrid + RRF + rerank.
- 2.3 Intent + bộ lọc, định tuyến SQL/OSRM.
- 2.4 Sinh câu trả lời có trích dẫn, hậu kiểm, xử lý trường hợp không có dữ liệu và mâu thuẫn.
- 2.5 Ngữ cảnh "Hỏi về địa điểm này".
- 2.6 Bộ đánh giá retrieval (§6).
- **Xong khi:** bộ câu hỏi mẫu đạt ngưỡng recall/nDCG đặt ra ở 2.6. Câu hỏi không có dữ liệu luôn được trả lời "chưa có dữ liệu".

### Phase 3: Lập lịch trình
- 3.1 Thu thập mong muốn + lưu `trips`.
- 3.2 Chọn ứng viên + ma trận OSRM + OR-Tools với ràng buộc khung giờ.
- 3.3 LLM viết ghi chú/lý do (không được đổi lịch).
- 3.4 Xóa / thay địa điểm (lọc → kiểm tra khả thi → LLM chọn).
- 3.5 `plan_qa` trong hỏi đáp (truy vấn SQL).
- **Xong khi:** lịch trình sinh ra không vi phạm giờ mở cửa hay khung thời gian trong ngày (có test). Thay một địa điểm không làm hỏng các item `locked`.

### Phase 4: Mở rộng
- 4.1 Thêm địa điểm từ video ngay trong màn chỉnh sửa lịch trình (màn review add/update).
- 4.2 `TikTokSource` (yt-dlp) qua `VideoSource`.
- 4.3 Tách worker theo queue (`media` / `llm` / `index`), model server cho embedding/rerank nếu cần.

## 8. Câu hỏi mở và rủi ro

| # | Vấn đề | Ghi chú |
|---|---|---|
| Q1 | Chọn LLM provider / model nào? | Cần hỗ trợ tốt structured output và tiếng Việt. Ảnh hưởng tới chi phí extraction |
| Q2 | Frontend dùng gì? | Map cần MapLibre/Leaflet + tile OSM. Chưa nằm trong plan |
| Q3 | API làm thế nào để embed câu hỏi và rerank khi Q&A cần trả lời ngay? | Hai lựa chọn: (a) API tự load bge-m3 + reranker, nặng hơn nhưng đơn giản; (b) dùng model server riêng (Infinity/TEI). Nên quyết ở Phase 2 |
| Q4 | Hạ tầng GPU | faster-whisper + PaddleOCR + bge-m3 trên CPU sẽ chậm. Cần ước lượng thời gian xử lý một video |
| Q5 | Nominatim công khai giới hạn 1 request/giây và cấm dùng nặng | Dev dùng instance công khai kèm cache. Production phải tự host Nominatim/Overpass |
| Q6 | OSM thiếu nhiều POI nhỏ (quán vỉa hè) | Cho phép tạo place không có `osm_id`, người dùng tự ghim lên map |
| Q7 | Chất lượng ASR khi video có nhạc nền to, giọng địa phương, nói lẫn Việt–Anh | Theo dõi bằng bộ video mẫu. Có thể thêm bước VAD hoặc tách giọng |
| Q8 | Trọng số và ngưỡng resolution | Cần dữ liệu thật để chỉnh. Để trong config, ghi log quyết định để phân tích sau |
| R1 | Chi phí LLM theo số video | Chỉ gửi chunk có khả năng nhắc tới địa điểm, cache theo hash của video |
| R2 | Quyền riêng tư (video là dữ liệu cá nhân) | Lọc `user_id` ở mọi truy vấn (+ RLS). Có chức năng xóa video kèm toàn bộ dữ liệu suy ra từ nó |
