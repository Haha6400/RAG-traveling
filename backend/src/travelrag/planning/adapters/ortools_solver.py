"""Itinerary as a prize-collecting VRP with time windows (OR-Tools routing).

- one vehicle per trip day; time is absolute minutes from the first day's 00:00
- a node's time windows are the union over days of its allowed start intervals; the gaps are
  removed from its time variable, so a stop can only be scheduled while it is open
- optional visits: nodes sharing a key form one disjunction (visit at most one of them) whose
  penalty reflects the place's score; a restaurant's lunch and dinner variants share a key
- per-day capacities: number of stops, one lunch, one dinner, and (optionally) budget
"""

import itertools

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from travelrag.planning.solver import SolverProblem, SolverSolution, SolverStop

DAY = 24 * 60


class OrToolsSolver:
    def solve(self, problem: SolverProblem) -> SolverSolution:
        n = len(problem.nodes)
        keys = list(dict.fromkeys(node.key for node in problem.nodes))
        if n == 0 or problem.days == 0:
            return SolverSolution(stops=[], dropped=keys)

        manager = pywrapcp.RoutingIndexManager(n + 1, problem.days, 0)
        routing = pywrapcp.RoutingModel(manager)

        def service(node: int) -> int:
            return 0 if node == 0 else problem.nodes[node - 1].service_min + problem.buffer_min

        def travel_cb(i: int, j: int) -> int:
            return int(problem.travel_min[manager.IndexToNode(i)][manager.IndexToNode(j)])

        def time_cb(i: int, j: int) -> int:
            a, b = int(manager.IndexToNode(i)), int(manager.IndexToNode(j))
            return service(a) + problem.travel_min[a][b]

        travel_index = routing.RegisterTransitCallback(travel_cb)
        routing.SetArcCostEvaluatorOfAllVehicles(travel_index)

        time_index = routing.RegisterTransitCallback(time_cb)
        horizon = problem.days * DAY
        routing.AddDimension(time_index, horizon, horizon, False, "Time")
        time = routing.GetDimensionOrDie("Time")

        for day in range(problem.days):
            start, end = routing.Start(day), routing.End(day)
            time.CumulVar(start).SetRange(
                day * DAY + problem.day_start, day * DAY + problem.day_end
            )
            time.CumulVar(end).SetRange(day * DAY + problem.day_start, day * DAY + problem.day_end)
            routing.AddVariableMinimizedByFinalizer(time.CumulVar(start))
            routing.AddVariableMinimizedByFinalizer(time.CumulVar(end))

        groups: dict[str, list[int]] = {}
        for node_idx, node in enumerate(problem.nodes, start=1):
            index = manager.NodeToIndex(node_idx)
            intervals = sorted(
                (day * DAY + s, day * DAY + e)
                for day, day_windows in enumerate(node.windows)
                for s, e in day_windows
                if s <= e
            )
            if not intervals:  # can never be visited
                routing.solver().Add(routing.ActiveVar(index) == 0)
            else:
                var = time.CumulVar(index)
                var.SetRange(intervals[0][0], intervals[-1][1])
                for (_, prev_end), (next_start, _) in itertools.pairwise(intervals):
                    if next_start - prev_end > 1:
                        var.RemoveInterval(prev_end + 1, next_start - 1)
            groups.setdefault(node.key, []).append(index)
        for indices in groups.values():
            penalty = max(problem.nodes[manager.IndexToNode(i) - 1].penalty for i in indices)
            routing.AddDisjunction(indices, penalty, 1)

        def add_capacity(name: str, demand: list[int], capacity: int) -> None:
            def cb(i: int) -> int:
                node = manager.IndexToNode(i)
                return 0 if node == 0 else demand[node - 1]

            idx = routing.RegisterUnaryTransitCallback(cb)
            routing.AddDimensionWithVehicleCapacity(idx, 0, [capacity] * problem.days, True, name)

        add_capacity("Stops", [1] * n, problem.max_stops)
        add_capacity("Lunch", [int(node.meal == "lunch") for node in problem.nodes], 1)
        add_capacity("Dinner", [int(node.meal == "dinner") for node in problem.nodes], 1)
        if problem.budget_per_day is not None:
            add_capacity("Budget", [node.cost for node in problem.nodes], problem.budget_per_day)

        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        params.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        params.time_limit.FromMilliseconds(int(problem.time_limit_seconds * 1000))

        solution = routing.SolveWithParameters(params)
        if solution is None:
            return SolverSolution(stops=[], dropped=keys)

        stops: list[SolverStop] = []
        visited: set[str] = set()
        for day in range(problem.days):
            index = solution.Value(routing.NextVar(routing.Start(day)))
            while not routing.IsEnd(index):
                node = manager.IndexToNode(index) - 1
                start = solution.Min(time.CumulVar(index)) - day * DAY
                stops.append(SolverStop(node=node, day=day, start_min=start))
                visited.add(problem.nodes[node].key)
                index = solution.Value(routing.NextVar(index))
        return SolverSolution(stops=stops, dropped=[k for k in keys if k not in visited])
