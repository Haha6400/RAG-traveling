"""Offline stand-ins for external services, used by tests and `seed --fake-models`.

- DemoGeocoder: answers from the Hanoi dataset instead of Nominatim/Overpass
- HashEmbedder: deterministic bag-of-words dense + sparse vectors (no model download)
- OverlapReranker: query/document word overlap

They exercise the same code paths as the real adapters; retrieval quality is of course
lower than with bge-m3 + the cross-encoder.
"""

import math
import re
import zlib

from travelrag.core.text import area_matches, normalize_name, unaccent
from travelrag.demo.hanoi import PLACES
from travelrag.geo.domain import BBox, OsmPlace
from travelrag.ml.ports import Embedding, SparseVector
from travelrag.places.domain import name_similarity

DENSE_DIM = 1024
SPARSE_DIM = 250002
_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = {
    "la", "va", "co", "cua", "o", "thi", "nhe", "khong", "nao", "gi", "the", "a", "an", "is",
    "are", "of", "to", "in", "at", "do", "does", "what", "where", "how", "nguoi", "review", "noi",
    "ve", "mot", "cho", "duoc", "rat", "nhung", "toi", "minh", "ban", "can", "i",
}  # fmt: skip


def tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(unaccent(text)) if w not in _STOP and len(w) > 1]


def _bucket(token: str, size: int) -> int:
    return zlib.crc32(token.encode()) % size


def osm_from_dataset(key: str) -> OsmPlace:
    p = PLACES[key]
    return OsmPlace(
        osm_type="node",
        osm_id=p["osm_id"],
        name=p["name"],
        lat=p["lat"],
        lng=p["lng"],
        category=p["category"],
        address=p["address"],
        opening_hours=p["opening_hours"],
    )


HANOI = BBox(20.95, 105.75, 21.10, 105.90)


class DemoGeocoder:
    """Behaves like Nominatim on the dataset: a district resolves to the box around its
    places, "Hà Nội" to the whole city, anything else to nothing; search honours the box."""

    async def area_bbox(self, area: str) -> BBox | None:
        inside = [
            p
            for p in PLACES.values()
            if area_matches(area, p["area"])
            and normalize_name(area) not in ("ha noi", "ha noi viet nam")
        ]
        if inside:
            return BBox(
                min(p["lat"] for p in inside) - 0.005, min(p["lng"] for p in inside) - 0.005,
                max(p["lat"] for p in inside) + 0.005, max(p["lng"] for p in inside) + 0.005,
            )  # fmt: skip
        return HANOI if "ha noi" in normalize_name(area) else None

    async def search(self, name: str, bbox: BBox | None, limit: int = 5) -> list[OsmPlace]:
        def inside(p: dict[str, float]) -> bool:
            return bbox is None or (
                bbox.south <= p["lat"] <= bbox.north and bbox.west <= p["lng"] <= bbox.east
            )

        scored = [(name_similarity(name, p["name"]), k) for k, p in PLACES.items() if inside(p)]
        hits = sorted((s, k) for s, k in scored if s >= 0.5)[::-1][:limit]
        return [osm_from_dataset(k) for _, k in hits]


class HashEmbedder:
    async def embed(self, texts: list[str]) -> list[Embedding]:
        out = []
        for text in texts:
            toks = tokens(text)
            dense = [0.0] * DENSE_DIM
            weights: dict[int, float] = {}
            for t in toks:
                dense[_bucket(t, DENSE_DIM)] += 1.0
                idx = _bucket(t, SPARSE_DIM)
                weights[idx] = weights.get(idx, 0.0) + 1.0
            norm = math.sqrt(sum(v * v for v in dense)) or 1.0
            dense = [v / norm for v in dense]
            total = sum(weights.values()) or 1.0
            items = sorted(weights.items())
            out.append(
                Embedding(dense, SparseVector([i for i, _ in items], [v / total for _, v in items]))
            )
        return out


class OverlapReranker:
    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        q = set(tokens(query))
        if not q:
            return [0.0] * len(documents)
        return [len(q & set(tokens(d))) / len(q) for d in documents]
