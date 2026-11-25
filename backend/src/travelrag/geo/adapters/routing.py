import logging

import httpx

from travelrag.geo.domain import (
    DETOUR_FACTOR,
    ESTIMATE_SPEED_KMH,
    LatLng,
    TravelMatrix,
    TravelMode,
    haversine_m,
)

logger = logging.getLogger(__name__)


class StraightLineRouter:
    """Estimates from straight-line distance x detour factor at typical urban speeds."""

    async def matrix(self, points: list[LatLng], mode: TravelMode) -> TravelMatrix:
        speed_ms = ESTIMATE_SPEED_KMH[mode] / 3.6
        distances = [[haversine_m(a, b) * DETOUR_FACTOR for b in points] for a in points]
        durations = [[d / speed_ms for d in row] for row in distances]
        return TravelMatrix(durations, distances, source="estimate")


class OsrmRouter:
    """OSRM `table` service (car profile). Walking times are not on this server, so walking
    uses the straight-line estimate; motorbike uses car times. Falls back to estimates when
    OSRM is unreachable."""

    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._fallback = StraightLineRouter()

    async def matrix(self, points: list[LatLng], mode: TravelMode) -> TravelMatrix:
        if mode is TravelMode.WALK or len(points) < 2:
            return await self._fallback.matrix(points, mode)
        coords = ";".join(f"{p.lng:.6f},{p.lat:.6f}" for p in points)
        url = f"{self._base_url}/table/v1/driving/{coords}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                response = await http.get(url, params={"annotations": "duration,distance"})
            data = response.json()
            if response.status_code != 200 or data.get("code") != "Ok":
                raise ValueError(data.get("message", response.status_code))
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("OSRM unavailable (%s); using straight-line estimates", exc)
            return await self._fallback.matrix(points, mode)
        estimate = await self._fallback.matrix(points, mode)
        durations = [
            [d if d is not None else estimate.durations_s[i][j] for j, d in enumerate(row)]
            for i, row in enumerate(data["durations"])
        ]
        distances = [
            [d if d is not None else estimate.distances_m[i][j] for j, d in enumerate(row)]
            for i, row in enumerate(data["distances"])
        ]
        return TravelMatrix(durations, distances, source="osrm")
