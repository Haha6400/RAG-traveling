"""Nominatim (geocoding + search) and Overpass (name search fallback).

The public instances allow ~1 request/second and require an identifying User-Agent; we
send a generic one (no personal data), throttle, and cache results in process memory.
Self-host Nominatim/Overpass for production use.
"""

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from travelrag.geo.domain import BBox, LatLng, OsmPlace, osm_category

logger = logging.getLogger(__name__)

_CATEGORY_KEYS = ("amenity", "tourism", "historic", "leisure", "shop")
_AREA_CATEGORIES = ("boundary", "place")
# A street-address match must be about street size (~1.5 km); a larger box means Nominatim
# fell back to a ward or city, whose centre would be a misleading pin.
MAX_ADDRESS_SPAN_DEG = 0.015


def _bbox(result: dict[str, Any]) -> BBox:
    south, north, west, east = (float(v) for v in result["boundingbox"])
    return BBox(south=south, west=west, north=north, east=east)


class _Throttle:
    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self._min_interval - (time.monotonic() - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


def _category_from_tags(tags: dict[str, str]) -> str | None:
    for key in _CATEGORY_KEYS:
        if key in tags:
            return osm_category(key, tags[key])
    return None


def _address_from_tags(tags: dict[str, str]) -> str | None:
    parts = [tags.get("addr:housenumber"), tags.get("addr:street"), tags.get("addr:district")]
    text = " ".join(p for p in parts if p)
    return text or None


class OsmGeocoder:
    def __init__(self, nominatim_url: str, overpass_url: str | None, user_agent: str) -> None:
        self._nominatim = nominatim_url.rstrip("/")
        self._overpass = overpass_url
        self._headers = {"User-Agent": user_agent, "Accept-Language": "vi,en"}
        self._throttle = _Throttle(1.1)
        self._cache: dict[tuple[Any, ...], Any] = {}

    async def _get_json(self, url: str, params: dict[str, Any]) -> Any:
        key = (url, tuple(sorted(params.items())))
        if key in self._cache:
            return self._cache[key]
        await self._throttle.wait()
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as http:
            response = await http.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        self._cache[key] = data
        return data

    async def area_bbox(self, area: str) -> BBox | None:
        try:
            results = await self._get_json(
                f"{self._nominatim}/search", {"q": area, "format": "jsonv2", "limit": 5}
            )
        except httpx.HTTPError as exc:
            logger.warning("Nominatim area lookup failed: %s", exc)
            return None
        # Only areas count. Free-text search ranks POIs too: "Phố cổ, Hà Nội" returns the
        # restaurant "Phở Cồ" first, whose box would hide every other place.
        for r in results:
            if r.get("category") in _AREA_CATEGORIES:
                return _bbox(r)
        return None

    async def geocode_address(self, address: str, bbox: BBox | None) -> LatLng | None:
        params: dict[str, Any] = {"q": address, "format": "jsonv2", "limit": 5}
        if bbox is not None:
            params["viewbox"] = f"{bbox.west},{bbox.north},{bbox.east},{bbox.south}"
            params["bounded"] = 1
        try:
            results = await self._get_json(f"{self._nominatim}/search", params)
        except httpx.HTTPError as exc:
            logger.warning("Nominatim address lookup failed: %s", exc)
            return None
        for r in results:
            box = _bbox(r)
            if max(box.north - box.south, box.east - box.west) <= MAX_ADDRESS_SPAN_DEG:
                return LatLng(float(r["lat"]), float(r["lon"]))
        return None

    async def search(self, name: str, bbox: BBox | None, limit: int = 5) -> list[OsmPlace]:
        places = await self._nominatim_search(name, bbox, limit)
        if not places and self._overpass and bbox is not None:
            places = await self._overpass_search(name, bbox, limit)
        return places

    async def _nominatim_search(self, name: str, bbox: BBox | None, limit: int) -> list[OsmPlace]:
        params: dict[str, Any] = {
            "q": name,
            "format": "jsonv2",
            "limit": limit,
            "extratags": 1,
            "namedetails": 1,
            "addressdetails": 1,
        }
        if bbox is not None:
            params["viewbox"] = f"{bbox.west},{bbox.north},{bbox.east},{bbox.south}"
            params["bounded"] = 1
        try:
            results = await self._get_json(f"{self._nominatim}/search", params)
        except httpx.HTTPError as exc:
            logger.warning("Nominatim search failed: %s", exc)
            return []
        places = []
        for r in results:
            extratags = r.get("extratags") or {}
            names = r.get("namedetails") or {}
            address = r.get("address") or {}
            road = " ".join(p for p in (address.get("house_number"), address.get("road")) if p)
            places.append(
                OsmPlace(
                    osm_type=r["osm_type"],
                    osm_id=int(r["osm_id"]),
                    name=names.get("name") or r.get("name") or name,
                    lat=float(r["lat"]),
                    lng=float(r["lon"]),
                    category=osm_category(r.get("category"), r.get("type")),
                    address=road or r.get("display_name"),
                    opening_hours=extratags.get("opening_hours"),
                    tags={k: str(v) for k, v in extratags.items()},
                )
            )
        return places

    async def _overpass_search(self, name: str, bbox: BBox, limit: int) -> list[OsmPlace]:
        assert self._overpass is not None
        pattern = re.escape(name).replace('"', "")
        box = f"{bbox.south},{bbox.west},{bbox.north},{bbox.east}"
        query = f'[out:json][timeout:25];nwr["name"~"{pattern}",i]({box});out center tags {limit};'
        await self._throttle.wait()
        try:
            async with httpx.AsyncClient(headers=self._headers, timeout=40) as http:
                response = await http.post(self._overpass, data={"data": query})
            response.raise_for_status()
            elements = response.json().get("elements", [])
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Overpass search failed: %s", exc)
            return []
        places = []
        for el in elements:
            tags = el.get("tags", {})
            center = el.get("center") or el
            if "lat" not in center:
                continue
            places.append(
                OsmPlace(
                    osm_type=el["type"],
                    osm_id=int(el["id"]),
                    name=tags.get("name", name),
                    lat=float(center["lat"]),
                    lng=float(center["lon"]),
                    category=_category_from_tags(tags),
                    address=_address_from_tags(tags),
                    opening_hours=tags.get("opening_hours"),
                    tags=tags,
                )
            )
        return places
