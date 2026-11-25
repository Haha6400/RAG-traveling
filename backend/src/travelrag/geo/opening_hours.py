"""A practical subset of the OSM `opening_hours` syntax.

Supported: `24/7`; rules separated by `;`; weekday selectors (`Mo-Fr`, `Sa,Su`, `Mo-Su`),
times (`08:00-22:00`, comma-separated ranges, ranges past midnight like `18:00-02:00`), `off`
/ `closed`, and `PH` (public holidays, ignored). Later rules override earlier ones for the
days they name, as in OSM. Anything else makes the whole value "unknown" (None) rather than
guessed.
"""

import re
from dataclasses import dataclass

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_TIME_RANGE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\+?$")
_DAY_TOKEN = re.compile(r"^(Mo|Tu|We|Th|Fr|Sa|Su)(?:-(Mo|Tu|We|Th|Fr|Sa|Su))?$")

Interval = tuple[int, int]  # minutes since 00:00 of the day, end may exceed 1440


@dataclass(frozen=True)
class WeekSchedule:
    """Open intervals per weekday (0 = Monday). Ends past 24:00 spill into the next day."""

    days: tuple[tuple[Interval, ...], ...]

    def intervals(self, weekday: int) -> list[Interval]:
        """Intervals that apply on `weekday`, including spill-over from the previous day."""
        own = [(s, min(e, 1440)) for s, e in self.days[weekday]]
        spill = [(0, e - 1440) for _, e in self.days[(weekday - 1) % 7] if e > 1440]
        return sorted(spill + own)

    def is_open(self, weekday: int, minute: int) -> bool:
        return any(s <= minute < e for s, e in self.intervals(weekday))

    def open_after(self, weekday: int, minute: int) -> bool:
        """Still open later than `minute` on that day (a range ending past midnight counts)."""
        return any(e > minute for _, e in self.days[weekday])

    def any_day_open_after(self, minute: int) -> bool:
        return any(self.open_after(d, minute) for d in range(7))


def _parse_days(token: str) -> list[int] | None:
    days: list[int] = []
    for part in token.split(","):
        part = part.strip()
        if part == "PH":
            continue
        m = _DAY_TOKEN.match(part)
        if not m:
            return None
        start = DAYS.index(m.group(1))
        end = DAYS.index(m.group(2)) if m.group(2) else start
        span = range(start, end + 1) if end >= start else [*range(start, 7), *range(0, end + 1)]
        days.extend(span)
    return days


def _parse_times(token: str) -> list[Interval] | None:
    out: list[Interval] = []
    for part in token.split(","):
        m = _TIME_RANGE.match(part.strip())
        if not m:
            return None
        h1, m1, h2, m2 = (int(g) for g in m.groups())
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if start >= 1440 or end > 1440 or m1 >= 60 or m2 >= 60:
            return None
        if end <= start:
            end += 1440  # past midnight
        out.append((start, end))
    return out


def parse(value: str | None) -> WeekSchedule | None:
    if not value or not value.strip():
        return None
    value = value.strip()
    if value == "24/7":
        return WeekSchedule(tuple(((0, 1440),) for _ in range(7)))

    week: list[list[Interval]] = [[] for _ in range(7)]
    for raw_rule in value.split(";"):
        rule = raw_rule.strip()
        if not rule:
            continue
        head, _, rest = rule.partition(" ")
        days = _parse_days(head)
        if days is None:  # no day selector: rule applies to every day
            days, rest = list(range(7)), rule
        rest = rest.strip()
        if rest in ("off", "closed"):
            for d in days:
                week[d] = []
            continue
        if not rest and days:
            return None
        times = [(0, 1440)] if rest == "24/7" else _parse_times(rest)
        if times is None:
            return None
        for d in days:
            week[d] = list(times)
    return WeekSchedule(tuple(tuple(d) for d in week))
