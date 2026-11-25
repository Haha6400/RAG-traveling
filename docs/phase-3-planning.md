# Phase 3 — Personal trip planning

`POST /api/trips` builds a day-by-day itinerary from the places on the user's map. Travel time,
opening hours and capacity are hard constraints handled by a solver. The LLM only writes notes
and picks between replacements that are already known to fit.

## Inputs

```json
{
  "title": "Hà Nội 2 ngày", "area": "Hà Nội",
  "start_date": "2026-11-23", "end_date": "2026-11-24",
  "preferences": {
    "budget": 600000, "currency": "VND",
    "pace": "balanced",                       // relaxed | balanced | tight
    "priorities": ["food", "culture"],        // food | photo | culture | shopping | nightlife
    "transport": "motorbike",                 // walk | motorbike | car
    "day_start": "08:00", "day_end": "21:30",
    "hotel_place_id": null,                   // a hotel on your map = start/end of each day
    "exclude_place_ids": []
  }
}
```

| Pace | Buffer after each visit | Max stops / day |
|---|---|---|
| relaxed | 30 min | 4 |
| balanced | 15 min | 6 |
| tight | 5 min | 8 |

Visit length by category: restaurant 60, cafe 45, bar 60, temple 40, attraction 75, shop 40,
other 45 minutes.

## Generation (`planning/service.py: create`)

1. **Candidates**: the user's places in the area, except hotels and excluded places. Places
   without coordinates or closed on every trip day are listed in `unscheduled` with a reason
   (`missing_location`, `closed_during_trip`).
2. **Opening windows** per day (`planning/domain.py: open_windows`): OSM-syntax hours from the
   card (or OSM), clipped to the day. Unknown hours count as open all day. Restaurants are
   further limited to meal slots: lunch 11:00–13:30, dinner 17:30–20:30.
3. **Travel matrix** (`geo`): OSRM `table` (car profile; walking uses estimates) or, without
   OSRM, straight-line distance × 1.35 at 4.5 / 20 / 16 km/h (walk / motorbike / car). The trip
   records `routing_source` = `osrm` or `estimate`, and every leg carries its source.
4. **Solver** (`planning/adapters/ortools_solver.py`): a prize-collecting vehicle routing
   problem with time windows (OR-Tools):
   - one vehicle per day; time is absolute minutes from day 1, 00:00
   - a node's allowed start times = union over days of its open windows minus its visit
     length; the gaps are removed from its time variable, so a stop is only ever scheduled
     while the place is open for the whole visit (e.g. never on the day a museum is closed)
   - transit = visit + buffer + travel; waiting is allowed
   - **optional visits**: each place is a disjunction whose penalty = 1000 × preference score
     (video recommendation, priority match, number of videos). Dropping a place costs its
     score, and the solver minimises travel time plus dropped value
   - **restaurants get a lunch variant and a dinner variant** in the same disjunction. Per-day
     capacities allow 1 lunch and 1 dinner (no two lunches in a row), `max_stops` stops, and
     `budget / days` in cost when a budget is set
   - search: path-cheapest-arc, then guided local search for
     `TRAVELRAG_PLANNER_TIME_LIMIT_SECONDS` (3 s)
   - with a hotel, the hotel is the depot (real travel to the first and from the last stop);
     otherwise the routes are open
5. **Items**: day, position, start/end, travel from the previous stop (distance, minutes,
   source), and cost estimate (midpoint of the card's price range).
6. **Notes**: one LLM call (effort `low`) gets the scheduled items plus each place's review
   brief, and returns one note per `item_id`. Unknown ids are ignored, and times, order and
   places are never taken from the model. Without an LLM key, notes are built from a template
   (strengths, price, best time).

## Editing

| Endpoint | Behaviour |
|---|---|
| `POST /trips/{id}/items/{item}/replace` `{}` | see below |
| `POST …/replace` `{"place_id": …}` | put a specific alternative into the slot (must be feasible) |
| `DELETE /trips/{id}/items/{item}` | remove a stop; positions and travel legs of that day are recomputed |
| `PATCH /trips/{id}/items/{item}` `{"locked": true}` | locked stops cannot be replaced or removed (409) |
| `DELETE /trips/{id}` | delete the trip |

**Replacement** (`replace_item`) is a filtered query plus a feasibility check. The trip is
not re-solved, so **no other stop moves**:
1. Filter: same category as the removed stop, within 3 km, similar price (not above 1.5× its
   max or below 0.5× its min), not already in the trip, has coordinates.
2. Feasibility: travel from the previous stop and to the next one (router), the buffer, the
   opening windows on that date and, for restaurants, the meal slot. A candidate qualifies only
   if a start time exists that fits between its neighbours.
3. The top 5 feasible options (score = preference − distance penalty) go to the LLM, which
   picks one and gives a one-sentence reason. Without an LLM, the best score wins. The other
   options are returned as `alternatives`, and choosing one calls the same endpoint with its
   `place_id`.
4. 409 when the stop is locked or nothing fits.

**Plan questions** (Phase 2 `plan_qa`) read the latest trip, or `trip_id`, as JSON: dates,
times, places, legs, costs, notes, unscheduled places.

## How to test

| File | Covers |
|---|---|
| `tests/unit/test_planner.py` | OR-Tools on hand-made problems: time windows + travel order, closed days across a 2-day trip, dropping the lowest-value nodes at capacity, one lunch + one dinner from 3 restaurants, budget capacity, impossible nodes; `open_windows` (meal slots, Monday closures, unknown hours, past midnight); `fits`, `intersect`; preference score |
| `tests/integration/test_phase3_trips.py` (seeded demo) | a 2-day trip from a Monday checked stop by stop: within the day, open for the whole visit, restaurants inside meal slots, ≤ 6 stops, ≤ 2 restaurants, travel time respected between stops, Hoàng thành not on Monday, no hotels as stops; LLM notes used but unknown ids ignored; per-day budget; hotel legs; invalid dates → 422; replace keeps category and every other stop unchanged and stays feasible; choosing an alternative; locked stops → 409; remove re-numbers positions; plan questions read the trip state; privacy and delete |

A sample itinerary from the demo data (2 days from Monday, priorities food + culture,
straight-line estimates):

```
Day 1 (Mon)  11:00 Phở Thìn Lò Đúc (lunch) → 12:18 Cộng Cà Phê → 13:19 Kem Tràng Tiền
             → 14:22 Đền Ngọc Sơn → 15:19 Cà phê Giảng → 17:30 Xôi Yến (dinner)
Day 2 (Tue)  08:00 Chùa Trấn Quốc → 09:01 Hoàng thành Thăng Long (closed Mondays)
             → 10:35 Văn Miếu → 12:10 Bún bò Nam Bộ (lunch) → 17:30 Chả cá Thăng Long (dinner)
             → 18:47 Phố đường tàu
```

Manual: see [testing.md](testing.md).
