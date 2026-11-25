import logging
import uuid
from dataclasses import dataclass

from pydantic import BaseModel

from travelrag.core.text import normalize_name
from travelrag.geo.domain import BBox, OsmPlace
from travelrag.geo.ports import Geocoder
from travelrag.jobs.ports import UnitOfWork
from travelrag.llm.ports import LLMRefusalError, LLMUnavailableError, StructuredLLM
from travelrag.places.domain import (
    Category,
    MatchCandidate,
    Mention,
    MentionSource,
    MentionStatus,
    Place,
    aggregate,
    card_text,
    match_score,
)
from travelrag.places.ports import JobResumer, PlaceCard, PlaceRepository

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 3


class PlaceNotFoundError(Exception):
    pass


class CandidateNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class ResolveSettings:
    auto_merge: float = 0.85
    ask_user: float = 0.60
    default_region: str = "Hà Nội, Việt Nam"


@dataclass(frozen=True)
class _Scored:
    score: float
    place: Place | None
    osm: OsmPlace | None


@dataclass(frozen=True)
class ReviewItem:
    mention: Mention
    candidates: list[tuple[MatchCandidate, Place | None]]


@dataclass(frozen=True)
class PlaceView:
    place: Place
    card: PlaceCard | None
    sources: list[MentionSource]


class _Summary(BaseModel):
    summary: str


SUMMARY_SYSTEM = """You summarize what several review videos say about one place.
Write 2-4 sentences in the language of the input notes. Use only the notes given; when videos
disagree, say so. No marketing tone, no information that is not in the notes."""


class PlacesService:
    def __init__(
        self,
        repo: PlaceRepository,
        geocoder: Geocoder,
        llm: StructuredLLM,
        uow: UnitOfWork,
        settings: ResolveSettings,
        resumer: JobResumer | None = None,
    ) -> None:
        self._repo = repo
        self._geocoder = geocoder
        self._llm = llm
        self._uow = uow
        self._settings = settings
        self._resumer = resumer

    # --- resolution (pipeline) ----------------------------------------------------------------

    async def add_mentions(self, mentions: list[Mention]) -> None:
        for mention in mentions:
            await self._repo.add_mention(mention)
        await self._uow.commit()

    async def resolve_video(self, video_id: uuid.UUID) -> tuple[set[uuid.UUID], bool]:
        """Resolve every pending mention of a video. Returns (touched place ids, needs review)."""
        touched: set[uuid.UUID] = set()
        for mention in await self._repo.mentions_for_video(video_id):
            if mention.status is MentionStatus.PENDING:
                place_id = await self._resolve(mention)
                if place_id:
                    touched.add(place_id)
                await self._uow.commit()
        mentions = await self._repo.mentions_for_video(video_id)
        touched |= {m.place_id for m in mentions if m.place_id}
        return touched, any(m.status is MentionStatus.REVIEW for m in mentions)

    async def _resolve(self, mention: Mention) -> uuid.UUID | None:
        scored = await self._score_candidates(mention)
        best = scored[0] if scored else None
        if best and best.score >= self._settings.auto_merge:
            return await self._attach(mention, best.place, best.osm)
        review = [s for s in scored if s.score >= self._settings.ask_user][:MAX_CANDIDATES]
        if review:
            for s in review:
                await self._repo.add_candidate(
                    MatchCandidate(
                        id=uuid.uuid4(),
                        user_id=mention.user_id,
                        mention_id=mention.id,
                        score=round(s.score, 3),
                        place_id=s.place.id if s.place else None,
                        osm=None if s.place else s.osm,
                    )
                )
            mention.status = MentionStatus.REVIEW
            await self._repo.update_mention(mention)
            return None
        return await self._attach(mention, None, None)

    async def _score_candidates(self, mention: Mention) -> list[_Scored]:
        by_target: dict[object, _Scored] = {}

        def keep(s: _Scored) -> None:
            key = s.place.id if s.place else (s.osm.osm_type, s.osm.osm_id) if s.osm else None
            if key is not None and (key not in by_target or by_target[key].score < s.score):
                by_target[key] = s

        for place in await self._repo.find_similar(mention.user_id, mention.name):
            score = match_score(
                mention.name,
                mention.area,
                mention.category,
                [place.name, *place.aliases],
                place.area or place.address,
                place.category.value,
            )
            keep(_Scored(score, place, None))

        try:
            bbox, in_mentioned_area = await self._search_area(mention)
            osm_results = await self._geocoder.search(mention.name, bbox)
        except Exception as exc:  # geocoding is best effort; never fail the pipeline on it
            logger.warning("geocoding failed for %r: %s", mention.name, exc)
            in_mentioned_area, osm_results = False, []
        # POIs found inside the mentioned area's bounding box agree on area by construction;
        # when only the default region was searched, the area is unknown.
        osm_area = mention.area if in_mentioned_area else None
        for osm in osm_results:
            existing = await self._repo.find_by_osm(mention.user_id, osm.osm_type, osm.osm_id)
            names = [osm.name, *(existing.aliases if existing else [])]
            score = match_score(
                mention.name,
                mention.area,
                mention.category,
                names,
                osm_area,
                osm.category,
            )
            keep(_Scored(score, existing, None if existing else osm))
        return sorted(by_target.values(), key=lambda s: s.score, reverse=True)

    async def _search_area(self, mention: Mention) -> tuple[BBox | None, bool]:
        """Box to search in: the mentioned area, else the default region (never the whole
        world, where a name like "Lan Can" matches anything). Second value: whether the box
        is the mentioned area's."""
        if mention.area:
            bbox = await self._geocoder.area_bbox(mention.area)
            if bbox is not None:
                return bbox, True
        return await self._geocoder.area_bbox(self._settings.default_region), False

    async def _locate_by_address(self, place: Place, mention: Mention) -> None:
        """A place that OSM does not know still gets a pin when the video gives its address.
        The point is street-level (OSM rarely has house numbers in Vietnam), so the user may
        move it; a later OSM match or the user's pin replaces it."""
        address = mention.payload.get("address_hint")
        if place.has_location or not address:
            return
        try:
            bbox, _ = await self._search_area(mention)
            point = await self._geocoder.geocode_address(address, bbox)
        except Exception as exc:
            logger.warning("address geocoding failed for %r: %s", address, exc)
            return
        if point is not None:
            place.lat, place.lng = point.lat, point.lng
            place.address = place.address or address

    async def _attach(
        self, mention: Mention, place: Place | None, osm: OsmPlace | None
    ) -> uuid.UUID:
        if place is None and osm is not None:
            place = await self._repo.find_by_osm(mention.user_id, osm.osm_type, osm.osm_id)
            if place is None:
                place = self._place_from_osm(mention, osm)
                await self._repo.add_place(place)
        if place is None:
            place = Place(
                id=uuid.uuid4(),
                user_id=mention.user_id,
                name=mention.name,
                category=mention.category,
                area=mention.area,
                address=mention.payload.get("address_hint"),
            )
            await self._locate_by_address(place, mention)
            await self._repo.add_place(place)
        else:
            place.add_alias(mention.name)
            await self._locate_by_address(place, mention)
            await self._repo.update_place(place)
        mention.place_id = place.id
        mention.status = MentionStatus.RESOLVED
        await self._repo.update_mention(mention)
        return place.id

    @staticmethod
    def _place_from_osm(mention: Mention, osm: OsmPlace) -> Place:
        category = mention.category
        if category is Category.OTHER and osm.category:
            category = Category(osm.category)
        place = Place(
            id=uuid.uuid4(),
            user_id=mention.user_id,
            name=osm.name,
            category=category,
            area=mention.area,
            address=osm.address or mention.payload.get("address_hint"),
            lat=osm.lat,
            lng=osm.lng,
            osm_type=osm.osm_type,
            osm_id=osm.osm_id,
            osm_opening_hours=osm.opening_hours,
        )
        place.add_alias(mention.name)
        return place

    # --- review (user) --------------------------------------------------------------------------

    async def pending_reviews(self, user_id: uuid.UUID) -> list[ReviewItem]:
        items = []
        for mention in await self._repo.mentions_in_review(user_id):
            candidates = [
                (c, await self._repo.get_place(c.place_id) if c.place_id else None)
                for c in await self._repo.candidates_for_mention(mention.id)
                if c.status == "pending"
            ]
            items.append(ReviewItem(mention, candidates))
        return items

    async def accept_candidate(self, candidate_id: uuid.UUID, user_id: uuid.UUID) -> Place:
        candidate = await self._repo.get_candidate(candidate_id)
        if candidate is None or candidate.user_id != user_id or candidate.status != "pending":
            raise CandidateNotFoundError(candidate_id)
        mention = await self._repo.get_mention(candidate.mention_id)
        assert mention is not None
        place = await self._repo.get_place(candidate.place_id) if candidate.place_id else None
        place_id = await self._attach(mention, place, candidate.osm)
        for other in await self._repo.candidates_for_mention(mention.id):
            other.status = "accepted" if other.id == candidate.id else "rejected"
            await self._repo.update_candidate(other)
        return await self._after_review(mention, place_id)

    async def reject_candidates(self, mention_id: uuid.UUID, user_id: uuid.UUID) -> Place:
        """None of the suggestions is right: the mention becomes a new place."""
        mention = await self._repo.get_mention(mention_id)
        if (
            mention is None
            or mention.user_id != user_id
            or mention.status is not MentionStatus.REVIEW
        ):
            raise CandidateNotFoundError(mention_id)
        for candidate in await self._repo.candidates_for_mention(mention.id):
            candidate.status = "rejected"
            await self._repo.update_candidate(candidate)
        place_id = await self._attach(mention, None, None)
        return await self._after_review(mention, place_id)

    async def _after_review(self, mention: Mention, place_id: uuid.UUID) -> Place:
        await self.rebuild_cards({place_id})
        await self._uow.commit()
        remaining = [
            m
            for m in await self._repo.mentions_for_video(mention.video_id)
            if m.status is MentionStatus.REVIEW
        ]
        if not remaining and self._resumer is not None:
            await self._resumer.resume_after_review(mention.video_id)
        place = await self._repo.get_place(place_id)
        assert place is not None
        return place

    # --- cards ----------------------------------------------------------------------------------

    async def rebuild_cards(self, place_ids: set[uuid.UUID]) -> None:
        for place_id in place_ids:
            place = await self._repo.get_place(place_id)
            if place is None:
                continue
            sources = await self._repo.sources_for_place(place_id)
            aggregated, conflicts = aggregate(place, sources)
            card = await self._repo.get_card(place_id)
            if card is None:
                card = PlaceCard(place_id, place.user_id, aggregated, conflicts, "")
            elif card.aggregated != aggregated or card.conflicts != conflicts:
                card.aggregated, card.conflicts = aggregated, conflicts
                card.summary_stale = True
                card.version += 1
            text = card_text(place, aggregated, conflicts, card.summary_text)
            if text != card.card_text and card.card_text:
                card.version += 1  # e.g. aliases changed: the index must follow the text
            card.card_text = text
            await self._repo.save_card(card)
        await self._uow.commit()

    async def ensure_summary(self, place_id: uuid.UUID, *, force: bool = False) -> PlaceCard | None:
        """LLM summary of the descriptive fields; runs only when stale (or forced)."""
        card = await self._repo.get_card(place_id)
        place = await self._repo.get_place(place_id)
        if card is None or place is None or not (card.summary_stale or force):
            return card
        notes = [
            f"{label}: {item['text']} (video {item['video_name']}, {item['date']})"
            for key, label in (("pros", "+"), ("cons", "-"), ("best_time", "best time"))
            for item in card.aggregated.get(key) or []
        ]
        if not notes:
            card.summary_text, card.summary_stale = None, False
        else:
            try:
                result = await self._llm.generate(
                    task="summary",
                    system=SUMMARY_SYSTEM,
                    prompt=f"Place: {place.name}\nNotes:\n" + "\n".join(notes),
                    output=_Summary,
                    effort="low",
                    max_tokens=2000,
                )
            except (LLMUnavailableError, LLMRefusalError) as exc:
                logger.info("summary for %s skipped: %s", place_id, exc)
                return card
            card.summary_text, card.summary_stale = result.summary.strip(), False
        card.version += 1
        card.card_text = card_text(place, card.aggregated, card.conflicts, card.summary_text)
        await self._repo.save_card(card)
        await self._uow.commit()
        return card

    # --- video removal ------------------------------------------------------------------------

    async def places_for_video(self, video_id: uuid.UUID) -> set[uuid.UUID]:
        return {m.place_id for m in await self._repo.mentions_for_video(video_id) if m.place_id}

    async def refresh_after_removal(
        self, user_id: uuid.UUID, place_ids: set[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Call after a video (and so its mentions) is gone. Places no video mentions any more
        are deleted; the others drop aliases that only that video used, lose their LLM summary
        (it may quote the deleted video) and get their card recomputed from what remains.
        Returns the places whose card changed, for re-indexing."""
        updated: set[uuid.UUID] = set()
        for place_id in place_ids:
            place = await self._repo.get_place(place_id)
            if place is None or place.user_id != user_id:
                continue
            sources = await self._repo.sources_for_place(place_id)
            if not sources:
                await self._repo.delete_place(place_id)
                continue
            said = {normalize_name(s.mention.name) for s in sources}
            place.aliases = [a for a in place.aliases if normalize_name(a) in said]
            await self._repo.update_place(place)
            card = await self._repo.get_card(place_id)
            if card is not None:
                card.summary_text, card.summary_stale = None, True
                await self._repo.save_card(card)
            updated.add(place_id)
        await self._uow.commit()
        await self.rebuild_cards(updated)
        updated |= await self._settle_orphaned_reviews(user_id)
        return updated

    async def _settle_orphaned_reviews(self, user_id: uuid.UUID) -> set[uuid.UUID]:
        """A mention waiting for review whose candidate places were all deleted can no longer
        be answered: it becomes a new place (and its job may continue)."""
        created: set[uuid.UUID] = set()
        for mention in await self._repo.mentions_in_review(user_id):
            pending = [
                c
                for c in await self._repo.candidates_for_mention(mention.id)
                if c.status == "pending"
            ]
            if not pending:
                created.add((await self.reject_candidates(mention.id, user_id)).id)
        return created

    # --- reads / edits --------------------------------------------------------------------------

    async def get_view(self, place_id: uuid.UUID, user_id: uuid.UUID) -> PlaceView:
        place = await self._repo.get_place(place_id)
        if place is None or place.user_id != user_id:
            raise PlaceNotFoundError(place_id)
        return PlaceView(
            place, await self._repo.get_card(place_id), await self._repo.sources_for_place(place_id)
        )

    async def set_location(
        self, place_id: uuid.UUID, user_id: uuid.UUID, lat: float, lng: float
    ) -> Place:
        place = await self._repo.get_place(place_id)
        if place is None or place.user_id != user_id:
            raise PlaceNotFoundError(place_id)
        place.lat, place.lng = lat, lng
        await self._repo.update_place(place)
        await self._uow.commit()
        return place
