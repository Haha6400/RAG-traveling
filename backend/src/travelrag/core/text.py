"""Text normalization shared by matching, trigram search and indexing."""

import re
import unicodedata

# Generic words that say what kind of place it is, not which one. Stripped before comparing
# names so "Quán Phở Thìn" and "Phở Thìn" match.
_GENERIC_WORDS = {
    "quan",
    "nha",
    "hang",
    "nhahang",
    "tiem",
    "cua",
    "cafe",
    "ca",
    "phe",
    "coffee",
    "cf",
    "restaurant",
    "shop",
    "store",
    "the",
    "khach",
    "san",
    "hotel",
    "chua",
    "den",
    "pagoda",
    "temple",
    "bao",
    "tang",
    "museum",
}
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def unaccent(text: str) -> str:
    """Lowercase and strip diacritics (Vietnamese-aware: đ -> d)."""
    text = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return _SPACES.sub(" ", stripped.lower()).strip()


def normalize_name(name: str) -> str:
    """Comparable place name: unaccented, punctuation removed, generic words dropped."""
    words = _NON_WORD.sub(" ", unaccent(name)).split()
    kept = [w for w in words if w not in _GENERIC_WORDS]
    return " ".join(kept or words)


def area_matches(a: str | None, b: str | None) -> bool | None:
    """True/False when both areas are known, None otherwise. Containment counts as a match
    ("Hoàn Kiếm" vs "Quận Hoàn Kiếm, Hà Nội")."""
    if not a or not b:
        return None
    na, nb = normalize_name(a), normalize_name(b)
    return na in nb or nb in na


def format_timestamp(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def format_hhmm(minutes_of_day: int) -> str:
    """Clock time from minutes since midnight (values past 24:00 wrap to the next day)."""
    minutes = minutes_of_day % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
