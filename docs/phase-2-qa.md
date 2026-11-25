# Phase 2 — Q&A over your own videos

`POST /api/ask {question, place_id?, trip_id?}` answers from the user's own data only: their
videos, the places extracted from them, and their trips.

## Pipeline (`qa/service.py`)

```
question
  │
  ├─ 1. interpret (Claude, structured output, effort low) ──► intent + filters
  │
  ├─ out_of_scope ─────────────► fixed reply, no retrieval
  ├─ plan_qa ──────────────────► trip as JSON (SQL) ─► Claude answers from that JSON only
  ├─ route ────────────────────► router (OSRM or labelled estimate) ─► templated answer, no LLM
  ├─ filtered_search ──────────► metadata filter on place cards ─► rerank only for order ─► answer
  └─ place_lookup / detail_qa ─► hybrid retrieval ─► rerank ─► answer
                                  (scoped to named places, or to place_id)
```

### 1. Interpretation
One LLM call returns `Interpretation`: the intent, the language (`vi`/`en`), a standalone
search query, the place names mentioned, and the filters (category, area, price min/max,
weekday, open at HH:MM, open after HH:MM, and route origin / destination / mode). Intent and
filters come from a single call rather than two separate ones, to cut latency and cost.

| Intent | Example |
|---|---|
| `place_lookup` | "Phở Thìn có gì ngon?" |
| `filtered_search` | "Quán nào mở muộn sau 11 giờ đêm?", "Cà phê dưới 40k ở Hoàn Kiếm" |
| `detail_qa` | "Người review có nói gì về chỗ ngồi không?" |
| `plan_qa` | "Ngày 2 đi đâu?", "Tổng chi phí bao nhiêu?" |
| `route` | "Từ Văn Miếu tới phở Thìn bao xa?" (added: distances never come from the LLM) |
| `out_of_scope` | "Thủ đô của Pháp là gì?" |

### 2. Metadata filters (`qa/domain.py: matches_filters`)
Filters run in code on the place cards **before** any vector search. A place that does not
pass the filters cannot reach the answer.
- category and area (containment on unaccented names)
- price: `price_max` keeps places whose cheapest price ≤ limit; `price_min` the reverse
- opening hours via the OSM parser: `open_at` (open at that minute) and `open_after` (still
  open later, ranges past midnight count), optionally for a given weekday
- **unknown values never match a filter that needs them**: a place with unknown hours is not
  returned for "open late"

For `filtered_search`, the filters decide which places qualify. The reranker is used only to
order them when there are more than 8.

### 3. Hybrid retrieval (`qa/adapters/sql_retriever.py`)
Six ranked lists in Postgres, each `WHERE user_id = :uid` (+ place scope):

| Branch | Chunks | Cards |
|---|---|---|
| dense | `dense <=> :q` (HNSW, cosine) | same |
| sparse (bge-m3 lexical weights) | `sparse <#> :q` (HNSW, inner product) | same |
| trigram | `word_similarity(:kw, search_text)` on unaccented text | same |

- The keywords for trigram are the place names from the question (or the question itself), so
  "pho thin" without diacritics or "Ginzan Onsen" still matches.
- `SET LOCAL hnsw.iterative_scan = relaxed_order` (pgvector ≥ 0.8) keeps the HNSW scan going
  until enough rows pass the user filter.
- **Place scope**: a chunk belongs to a place when it overlaps one of that place's mentions
  (± 30 s).
- **RRF** (k = 60) fuses the six lists by rank, so chunk and card scores never need comparing.
  The top 20 go to the cross-encoder (bge-reranker-v2-m3). Results below
  `TRAVELRAG_RERANK_MIN_SCORE` are dropped and the best 6 are kept.

### 4. Answer
- Sources are labelled `[S1]…[Sn]`. Chunks carry their video name, date, time range and place.
  Cards carry the aggregated facts and their conflicts (with dates).
- The prompt requires an inline citation for every claim, `insufficient=true` when the sources
  do not answer, both versions with dates when sources disagree, and the question's language.
- Post-processing: citations to labels that were not provided are deleted, and the response
  lists only the cited sources (each with video id + timestamp to reopen the exact segment)
  and the places they refer to (id, name, coordinates).

### Special cases

| Case | Behaviour |
|---|---|
| Nothing retrieved / no place passes the filters / named place not on the map | `no_data`, fixed sentence, **no generation call** |
| Model says the sources are insufficient | `no_data`, no sources returned |
| Conflicting videos | both values with video and date reach the model; the prompt forbids choosing |
| Plan question | structured trip state (days, times, legs, costs, notes) as JSON, not retrieval |
| Distance / travel time | routing engine or a labelled straight-line estimate; no LLM |
| No LLM key | intent falls back to `detail_qa`; the answer lists the most relevant passages (`llm_used=false`) |
| ml-service down | `503` with the command that starts it |
| Another user's data | impossible by construction: every SQL branch filters by `user_id` |

## Evaluation (`python -m travelrag.qa.eval`)
`demo/hanoi.py: QA_EVAL` holds 8 labelled questions: Vietnamese and English, with and
without diacritics, place-specific and filter-type. The eval measures **retrieval only**:
recall@20 of the fused candidates and nDCG@5 after reranking.

Measured on the seeded demo (2026-11-23):

| Models | recall@20 | nDCG@5 |
|---|---|---|
| bge-m3 + bge-reranker-v2-m3 (ml-service, Apple GPU) | **0.97** | **0.84** |
| hash embedder + word overlap (`--fake-models`) | 0.94 | 0.57 |

English questions over Vietnamese transcripts score 1.0 nDCG@5 with bge-m3. The low score
for "Quán nào mở muộn?" is expected: that question is answered by the metadata filter, which
`tests/integration/test_phase2_qa.py` checks separately. Re-run the eval after changing
chunking, weights or models.

## How to test

| File | Covers |
|---|---|
| `tests/unit/test_qa_domain.py` | RRF, citation cleanup, every filter incl. weekday, past-midnight and unknown values |
| `tests/integration/test_phase2_qa.py` (seeded demo, scripted LLM) | detail question cites the seating segment (video + 00:32–01:06) and drops a fake label; retrieval scoped to the named place; "open after 23:00" returns exactly the two qualifying places; filter with no match → no LLM call; conflicting hours reach the model with dates; unknown place and out-of-scope; "Ask about this place" scope; route answer from the router; LLM-less fallback; insufficient → no data; no cross-user leakage |

Manual: see [testing.md](testing.md).
