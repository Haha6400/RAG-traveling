# Frontend and the personal map

## The app

`frontend/` is a Vue 3 + Vite single-page app with vue-router:

| Route | View | What it does |
|---|---|---|
| `/login` | `LoginView` | log in / register (every other route redirects here without a valid token) |
| `/` | `MapView` | **the personal map**: filterable place list, map, place card, "to confirm" reviews, pinning |
| `/videos` | `VideosView` | upload, processing progress (SSE), retry, delete |
| `/ask` | `AskView` | Q&A; `?place=<id>&name=…` scopes it to one place |
| `/trips` | `TripsView` | build and edit itineraries |
| `/dev` | `DevView` | dummy jobs, all jobs, raw SSE log (the old Phase 0 console) |

Shared pieces: `session.js` (token + user, used by the router guard), `composables/useJobs.js`
(one SSE stream per job, shared by Videos and Dev), `components/ClipPlayer.vue` (plays a video
from a timestamp; any view calls the injected `playClip`).

## The map (step 1 + 2)

`MapView` = sidebar | map | place card.

- **Markers**: one `L.circleMarker` per place with coordinates, colour by category
  (`lib/format.js: CATEGORY_COLORS`); dashed outline when videos disagree; the selected place
  is larger with a dark ring. Hover shows the name; click opens the card.
- **Filters** (client-side, on `/api/places`): name/alias search without diacritics
  ("pho thin" finds "Phở Thìn"), category chips with counts, area, max price, "only conflicting
  info". As in Q&A, a place with an unknown price never passes a price limit.
- **Card**: price, hours (with source), best time, strengths, weaknesses, rating, LLM summary,
  conflicts with video and date, and the source videos with ▶ timestamp buttons. "Ask about this
  place" opens `/ask?place=…`; Q&A answers link back with `/?place=…`.
- **Pinning (step 2)**: places OSM did not know are listed with a **Pin** button (and a hint
  on their card). Pin → the cursor becomes a crosshair and a banner appears → click the map →
  `PATCH /api/places/{id}/location`. **Move pin** does the same for a place that already has a
  location, after a confirmation. Esc cancels.
- The selected place is kept in the URL (`/?place=<id>`), so links and reloads reopen it.
- Below 900 px wide the layout stacks: list, then map, then card.

All Leaflet code is in `components/PlaceMap.vue`. Its interface is `places`, `selectedId`,
`pinTarget` in, and `select(id)`, `pick({lat, lng})` out. Nothing else in the app knows
which map library is used.

## Decision: Leaflet + OSM raster tiles now, MapLibre GL JS + OpenFreeMap later

### Why Leaflet first
- **It covers the current scope with the least code.** Steps 1–2 need markers, a click
  handler and a crosshair mode, which are a few lines with Leaflet's imperative API. The whole
  component is about 120 lines and needs no plugins.
- **Small and undemanding.** About 40 KB gzip, against about 200 KB for MapLibre. No WebGL, so
  it runs anywhere, including headless browsers in automated checks and old phones.
- **No account and no key.** Raster tiles come straight from `tile.openstreetmap.org`, the
  same data the backend already resolves places against.
- **The data is small.** A user has tens to a few hundred places, and SVG circle markers
  handle that without clustering or GPU rendering.

### Why move to MapLibre GL JS + OpenFreeMap later
- **Tile usage policy.** `tile.openstreetmap.org` is a donated service meant for light use. Its
  [tile usage policy](https://operations.osmfoundation.org/policies/tiles/) forbids heavy use
  and requires a valid Referer/User-Agent and visible attribution. That is fine for development
  and personal use, but not for a public deployment. [OpenFreeMap](https://openfreemap.org)
  serves OSM vector tiles without a key or registration, so switching keeps the "no account
  needed" property.
- **The next map features are vector-layer features.** Trip routes per day (OSRM geometry),
  highlighting the places in a Q&A answer, clustering, and density views are GeoJSON sources
  plus styled layers in MapLibre, with built-in clustering and data-driven styling. In Leaflet
  each needs a plugin and one DOM/SVG node per feature, which slows down with thousands of
  features.
- **Better map, smaller downloads.** Vector tiles stay sharp on high-DPI screens, zoom
  smoothly, use less bandwidth than raster tiles, and can be restyled. For example, base-map
  POIs can be dimmed so the user's own places stand out, and Vietnamese labels can be
  preferred.

### When to switch
Switch at whichever comes first:
- before any public / production deployment (tile policy)
- when starting map step 4 (trip routes) or step 5 (Q&A highlights)
- when a user's map passes about 1–2k markers

### How
Rewrite `PlaceMap.vue` on MapLibre GL JS with the same props and events:
- a GeoJSON source for the places, with a circle layer coloured by `category` (data-driven
  style) and clustering enabled
- the OpenFreeMap style URL
- a `click` handler on the map for pinning

The views and the backend stay as they are. `/api/places` already returns coordinates and
categories, and a `/api/places.geojson?bbox=` endpoint can be added if maps get large.

## How to test

Manual (with the demo data, see [testing.md](testing.md)):
1. Open http://localhost:5173. You are redirected to `/login`. Log in as `demo@example.com`.
2. The map fits the Hanoi places. Each colour is a category, and Cà phê Giảng has a dashed
   outline (conflicting hours).
3. Type `pho thin` (no diacritics): one place is left in the list and on the map.
4. Click a marker or a list entry: the card opens and the URL gets `?place=…`.
5. Pin a place that has no location (there are some after uploading a real video, see
   testing.md): **Pin**, then click the map. The marker appears and the place can now be used
   in trips.
6. On a narrow window the page stacks without horizontal scrolling.

The same steps were run headless with Playwright while building this (guard redirect, marker
count = places with coordinates, search, category filter, card + URL, deep link, pinning, mobile
layout, no console errors).

Docker note: on macOS with Colima, file events from the host do not reach containers, so the
`web` (Vite) and `api` (uvicorn) containers poll for changes (`VITE_USE_POLLING`,
`WATCHFILES_FORCE_POLLING` in `docker-compose.yml`).
