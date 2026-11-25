"""Solver input/output types and port. The planner builds a SolverProblem; an adapter (OR-Tools)
returns a SolverSolution. Times are minutes of day; `windows[d]` lists the intervals in which a
visit to the node may START on day d (already shortened by the visit duration).
Nodes with the same `key` (e.g. a restaurant's lunch and dinner variants) are alternatives:
at most one of them is visited."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class SolverNode:
    key: str
    service_min: int
    windows: list[list[tuple[int, int]]]  # per day: allowed START intervals
    penalty: int  # cost of leaving it out; higher = more important
    meal: str | None = None  # "lunch" | "dinner": at most one of each per day
    cost: int = 0  # in budget units (e.g. thousands)


@dataclass(frozen=True)
class SolverProblem:
    days: int
    day_start: int
    day_end: int
    nodes: list[SolverNode]
    travel_min: list[list[int]]  # (n+1) x (n+1); index 0 is the depot (hotel or "anywhere")
    buffer_min: int
    max_stops: int
    budget_per_day: int | None = None
    time_limit_seconds: float = 3.0


@dataclass(frozen=True)
class SolverStop:
    node: int  # index into problem.nodes
    day: int
    start_min: int


@dataclass(frozen=True)
class SolverSolution:
    stops: list[SolverStop]  # ordered by day, then visit order
    dropped: list[str] = field(default_factory=list)  # keys with no visited node


class ItinerarySolver(Protocol):
    def solve(self, problem: SolverProblem) -> SolverSolution: ...
