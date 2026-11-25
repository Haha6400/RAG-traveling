"""Question answering over the user's own data.

ask() pipeline:
 1. interpret (LLM, structured): intent + filters
 2. route by intent: plan -> trip data; route -> travel matrix; out_of_scope -> fixed reply
 3. metadata filters (SQL-free, on place cards) before any vector search
 4. hybrid retrieval: dense + sparse + trigram, fused with RRF, user-scoped
 5. cross-encoder rerank, keep the best few
 6. answer from labelled sources only; drop citations that point nowhere
"""

import json
import logging
import uuid

from travelrag.core.text import format_timestamp
from travelrag.geo.domain import LatLng, TravelMode
from travelrag.geo.ports import RouteProvider
from travelrag.llm.ports import LLMRefusalError, LLMUnavailableError, StructuredLLM
from travelrag.ml.ports import Embedder, Reranker
from travelrag.places.domain import name_similarity
from travelrag.places.ports import CatalogEntry, PlaceCatalog
from travelrag.qa.domain import (
    LLM_MISSING,
    NO_DATA,
    NO_TRIP,
    OUT_OF_SCOPE,
    Answer,
    Doc,
    GeneratedAnswer,
    Intent,
    Interpretation,
    Source,
    clean_citations,
    has_filters,
    matches_filters,
    rrf,
)
from travelrag.qa.ports import Retriever, TripReader

logger = logging.getLogger(__name__)

PLACE_NAME_MATCH = 0.75
MAX_FILTERED_DOCS = 8

INTERPRET_SYSTEM = """You classify questions a traveller asks about THEIR OWN saved data: travel
review videos they uploaded, the places extracted from them, and their trip plans.
Fill every field; use null when the question does not say. Do not guess filters that are not
asked for. A question about general world knowledge, or unrelated to travel places, videos or
their trip, is out_of_scope."""

ANSWER_SYSTEM = """You answer a traveller's question using ONLY the numbered sources provided
(excerpts of their own review videos and summaries of places they saved).
- Cite every claim inline with its source label, e.g. [S2].
- If the sources do not contain the answer, set insufficient=true and say so briefly; never
  use outside knowledge and never guess.
- If sources disagree (e.g. different opening hours), present each version with its video and
  date; do not pick one.
- Answer in the language of the question, concisely."""

PLAN_SYSTEM = """You answer questions about the traveller's trip plan, given as JSON. Use only
that data (days, times, places, travel legs, costs, notes). If the plan does not contain the
answer, say so. Answer in the language of the question, concisely. Times are local."""


def _fallback_interpretation(question: str) -> Interpretation:
    return Interpretation(
        intent=Intent.DETAIL_QA,
        language="vi",
        search_query=question,
        place_names=[],
        category=None,
        area=None,
        price_min=None,
        price_max=None,
        weekday=None,
        open_at=None,
        open_after=None,
        route_from=None,
        route_to=None,
        travel_mode=None,
    )


def _lang(value: str) -> str:
    return "en" if value.lower().startswith("en") else "vi"


class QAService:
    def __init__(
        self,
        llm: StructuredLLM,
        embedder: Embedder,
        reranker: Reranker,
        retriever: Retriever,
        catalog: PlaceCatalog,
        trips: TripReader,
        router: RouteProvider,
        *,
        candidates: int = 20,
        keep: int = 6,
        min_score: float = 0.05,
    ) -> None:
        self._llm = llm
        self._embedder = embedder
        self._reranker = reranker
        self._retriever = retriever
        self._catalog = catalog
        self._trips = trips
        self._router = router
        self._candidates = candidates
        self._keep = keep
        self._min_score = min_score

    async def ask(
        self,
        user_id: uuid.UUID,
        question: str,
        *,
        place_id: uuid.UUID | None = None,
        trip_id: uuid.UUID | None = None,
    ) -> Answer:
        question = question.strip()
        llm_ok = True
        try:
            q = await self._llm.generate(
                task="interpret",
                system=INTERPRET_SYSTEM,
                prompt=question,
                output=Interpretation,
                effort="low",
                max_tokens=2000,
            )
        except (LLMUnavailableError, LLMRefusalError) as exc:
            logger.info("interpretation fallback: %s", exc)
            q, llm_ok = _fallback_interpretation(question), False
        lang = _lang(q.language)
        entries = {e.place.id: e for e in await self._catalog.catalog(user_id)}

        if place_id is not None:  # "Ask about this place": everything is scoped to it
            if place_id not in entries:
                return Answer(NO_DATA[lang], q.intent, lang, no_data=True, llm_used=llm_ok)
            if q.intent in (Intent.FILTERED_SEARCH, Intent.OUT_OF_SCOPE, Intent.ROUTE):
                q.intent = Intent.PLACE_LOOKUP
            return await self._retrieve_and_answer(
                user_id, question, q, lang, [place_id], entries, llm_ok
            )

        if q.intent is Intent.OUT_OF_SCOPE:
            return Answer(OUT_OF_SCOPE[lang], q.intent, lang, llm_used=llm_ok)
        if q.intent is Intent.PLAN_QA:
            return await self._plan_answer(user_id, question, q, lang, trip_id)
        if q.intent is Intent.ROUTE and q.route_from and q.route_to:
            return await self._route_answer(q, lang, entries)

        if q.intent is Intent.FILTERED_SEARCH or (
            q.intent is not Intent.PLACE_LOOKUP and has_filters(q)
        ):
            matched = [e for e in entries.values() if matches_filters(e, q)]
            if not matched:
                return Answer(
                    NO_DATA[lang], Intent.FILTERED_SEARCH, lang, no_data=True, llm_used=llm_ok
                )
            return await self._answer_from_cards(question, q, lang, matched, llm_ok)

        scope = None
        if q.place_names:
            scope = self._match_places(q.place_names, entries)
            if q.intent is Intent.PLACE_LOOKUP and not scope:
                return Answer(NO_DATA[lang], q.intent, lang, no_data=True, llm_used=llm_ok)
        return await self._retrieve_and_answer(
            user_id, question, q, lang, scope or None, entries, llm_ok
        )

    # --- helpers --------------------------------------------------------------------------------

    @staticmethod
    def _match_places(names: list[str], entries: dict[uuid.UUID, CatalogEntry]) -> list[uuid.UUID]:
        found = []
        for name in names:
            best, best_score = None, 0.0
            for e in entries.values():
                score = max(name_similarity(name, n) for n in [e.place.name, *e.place.aliases])
                if score > best_score:
                    best, best_score = e.place.id, score
            if best is not None and best_score >= PLACE_NAME_MATCH and best not in found:
                found.append(best)
        return found

    async def _retrieve(
        self, user_id: uuid.UUID, q: Interpretation, scope: list[uuid.UUID] | None
    ) -> list[tuple[Doc, float]]:
        query = q.search_query or ""
        [embedding] = await self._embedder.embed([query])
        keywords = " ".join(q.place_names) or query
        rankings = await self._retriever.ranked(
            user_id, query=embedding, keywords=keywords, place_ids=scope, limit=self._candidates
        )
        keys = rrf(rankings)[: self._candidates]
        if not keys:
            return []
        docs = await self._retriever.load(user_id, keys)
        ordered = [docs[k] for k in keys if k in docs]
        scores = await self._reranker.rerank(query, [d.text for d in ordered])
        ranked = sorted(zip(ordered, scores, strict=True), key=lambda p: p[1], reverse=True)
        return [(d, s) for d, s in ranked if s >= self._min_score][: self._keep]

    async def _retrieve_and_answer(
        self,
        user_id: uuid.UUID,
        question: str,
        q: Interpretation,
        lang: str,
        scope: list[uuid.UUID] | None,
        entries: dict[uuid.UUID, CatalogEntry],
        llm_ok: bool,
    ) -> Answer:
        docs = await self._retrieve(user_id, q, scope)
        if not docs:
            return Answer(NO_DATA[lang], q.intent, lang, no_data=True, llm_used=llm_ok)
        return await self._generate(question, q, lang, docs, entries, llm_ok)

    async def _answer_from_cards(
        self, question: str, q: Interpretation, lang: str, matched: list[CatalogEntry], llm_ok: bool
    ) -> Answer:
        """Filters decide WHICH places qualify; the reranker only orders them."""
        docs = [
            Doc(
                key=f"p:{e.place.id}",
                kind="card",
                text=e.card.card_text if e.card else e.place.name,
                place_id=e.place.id,
                place_name=e.place.name,
            )
            for e in matched
        ]
        if len(docs) > MAX_FILTERED_DOCS:
            scores = await self._reranker.rerank(q.search_query or question, [d.text for d in docs])
            pairs = sorted(zip(docs, scores, strict=True), key=lambda p: p[1], reverse=True)
            ranked = pairs[:MAX_FILTERED_DOCS]
        else:
            ranked = [(d, 1.0) for d in docs]
        entries = {e.place.id: e for e in matched}
        return await self._generate(
            question, q, lang, ranked, entries, llm_ok, Intent.FILTERED_SEARCH
        )

    async def _generate(
        self,
        question: str,
        q: Interpretation,
        lang: str,
        docs: list[tuple[Doc, float]],
        entries: dict[uuid.UUID, CatalogEntry],
        llm_ok: bool,
        intent: Intent | None = None,
    ) -> Answer:
        labelled = {f"S{i}": (d, s) for i, (d, s) in enumerate(docs, start=1)}
        sources = {label: self._source(label, d, s) for label, (d, s) in labelled.items()}
        places = self._places_of([d for d, _ in docs], entries)
        intent = intent or q.intent
        if not llm_ok:
            listing = "\n".join(f"[{label}] {src.snippet}" for label, src in sources.items())
            return Answer(
                f"{LLM_MISSING[lang]}\n{listing}",
                intent,
                lang,
                list(sources.values()),
                places,
                llm_used=False,
            )

        context = "\n\n".join(self._render(label, d) for label, (d, _) in labelled.items())
        try:
            result = await self._llm.generate(
                task="answer",
                system=ANSWER_SYSTEM,
                prompt=f"Question: {question}\n\nSources:\n{context}",
                output=GeneratedAnswer,
                effort="medium",
                max_tokens=4000,
            )
        except (LLMUnavailableError, LLMRefusalError) as exc:
            logger.info("answer generation failed: %s", exc)
            listing = "\n".join(f"[{label}] {src.snippet}" for label, src in sources.items())
            return Answer(
                f"{LLM_MISSING[lang]}\n{listing}",
                intent,
                lang,
                list(sources.values()),
                places,
                llm_used=False,
            )

        text, used = clean_citations(result.answer, set(labelled))
        used += [c for c in result.cited if c in labelled and c not in used]
        if result.insufficient:
            return Answer(text or NO_DATA[lang], intent, lang, [], [], no_data=True)
        cited = [sources[label] for label in used] or list(sources.values())[:3]
        return Answer(text, intent, lang, cited, places)

    @staticmethod
    def _render(label: str, d: Doc) -> str:
        if d.kind == "chunk":
            when = f"{format_timestamp(d.start_ms or 0)}-{format_timestamp(d.end_ms or 0)}"
            date = d.video_date.date().isoformat() if d.video_date else "?"
            head = f"[{label}] Video '{d.video_name}' ({date}), {when}"
            if d.place_name:
                head += f", about {d.place_name}"
        else:
            head = f"[{label}] Place card: {d.place_name}"
        return f"{head}\n{d.text}"

    @staticmethod
    def _source(label: str, d: Doc, score: float) -> Source:
        snippet = d.text if len(d.text) <= 280 else d.text[:277] + "..."
        return Source(
            label,
            d.kind,
            d.place_id,
            d.place_name,
            d.video_id,
            d.video_name,
            d.start_ms,
            d.end_ms,
            snippet,
            round(score, 4),
        )

    @staticmethod
    def _places_of(
        docs: list[Doc], entries: dict[uuid.UUID, CatalogEntry]
    ) -> list[dict[str, object]]:
        seen: dict[uuid.UUID, dict[str, object]] = {}
        for d in docs:
            if d.place_id and d.place_id in entries and d.place_id not in seen:
                p = entries[d.place_id].place
                seen[p.id] = {"id": str(p.id), "name": p.name, "lat": p.lat, "lng": p.lng}
        return list(seen.values())

    async def _plan_answer(
        self,
        user_id: uuid.UUID,
        question: str,
        q: Interpretation,
        lang: str,
        trip_id: uuid.UUID | None,
    ) -> Answer:
        trip = await self._trips.trip_context(user_id, trip_id)
        if trip is None:
            return Answer(NO_TRIP[lang], Intent.PLAN_QA, lang, no_data=True)
        try:
            result = await self._llm.generate(
                task="plan_answer",
                system=PLAN_SYSTEM,
                prompt=f"Question: {question}\n\nTrip plan:\n{json.dumps(trip, ensure_ascii=False, indent=1)}",
                output=GeneratedAnswer,
                effort="low",
                max_tokens=3000,
            )
        except (LLMUnavailableError, LLMRefusalError):
            return Answer(
                json.dumps(trip, ensure_ascii=False, indent=1), Intent.PLAN_QA, lang, llm_used=False
            )
        return Answer(result.answer, Intent.PLAN_QA, lang, no_data=result.insufficient)

    async def _route_answer(
        self, q: Interpretation, lang: str, entries: dict[uuid.UUID, CatalogEntry]
    ) -> Answer:
        """Distances come from the routing engine (or a labelled estimate), never from the LLM."""
        ends = []
        for name in (q.route_from, q.route_to):
            ids = self._match_places([name or ""], entries)
            place = entries[ids[0]].place if ids else None
            if place is None or not place.has_location:
                missing = name or "?"
                msg = (
                    f"Mình không tìm thấy vị trí của '{missing}' trong bản đồ của bạn."
                    if lang == "vi"
                    else f"I can't find a location for '{missing}' on your map."
                )
                return Answer(msg, Intent.ROUTE, lang, no_data=True)
            ends.append(place)
        mode = q.travel_mode or TravelMode.MOTORBIKE
        a, b = ends
        matrix = await self._router.matrix([LatLng(a.lat, a.lng), LatLng(b.lat, b.lng)], mode)  # type: ignore[arg-type]
        km = matrix.distances_m[0][1] / 1000
        minutes = round(matrix.durations_s[0][1] / 60)
        estimated = matrix.source != "osrm"
        if lang == "vi":
            modes = {"walk": "đi bộ", "motorbike": "xe máy", "car": "ô tô"}
            text = (
                f"Từ {a.name} đến {b.name}: khoảng {km:.1f} km, ~{minutes} phút {modes[mode.value]}"
            )
            text += " (ước lượng theo đường chim bay)." if estimated else " (theo OSRM)."
        else:
            text = f"From {a.name} to {b.name}: about {km:.1f} km, ~{minutes} min by {mode.value}"
            text += " (straight-line estimate)." if estimated else " (OSRM)."
        places = [{"id": str(p.id), "name": p.name, "lat": p.lat, "lng": p.lng} for p in ends]
        return Answer(text, Intent.ROUTE, lang, places=places, llm_used=True)
