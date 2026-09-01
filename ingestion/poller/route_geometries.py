"""Route geometry generation for the SEPTA dashboard.

Builds one full-coverage polyline per bus/trolley route (every stop the route
serves lies on its line) using a "spider" walk over the stop graph derived from
the GTFS static tables.

This module operates on the local static feed dict (post-Neon) — no database.
The poller calls `build_geometries(data)` on static refresh to emit
`public/geometries.json`.
"""

import math
from collections import defaultdict

# Bus + trolley scope, matching the aggregation filter. Type 11 (trolleybus)
# covers SEPTA routes 59/66/75, which carry real real-time data.
ROUTE_SCOPE_TYPES = (0, 3, 11)

# Consecutive stops farther apart than this are deadhead/skip movements (e.g. a
# trip that jumps straight from the southern terminus to Doylestown), not real
# routing. Excluding them keeps the spider line on actual stop-to-stop edges.
_MAX_EDGE_M = 4000.0
_M_PER_DEG = 111_000.0


def _edge_ok(a, b, stop_coords):
    lat1, lon1 = stop_coords[a]
    lat2, lon2 = stop_coords[b]
    dlat = (lat1 - lat2) * _M_PER_DEG
    dlon = (lon1 - lon2) * _M_PER_DEG * math.cos(math.radians((lat1 + lat2) / 2))
    return dlat * dlat + dlon * dlon <= _MAX_EDGE_M * _MAX_EDGE_M


def _dist2(a, b, stop_coords):
    lat1, lon1 = stop_coords[a]
    lat2, lon2 = stop_coords[b]
    dlat = (lat1 - lat2) * _M_PER_DEG
    dlon = (lon1 - lon2) * _M_PER_DEG * math.cos(math.radians((lat1 + lat2) / 2))
    return dlat * dlat + dlon * dlon


def _components(nodes, graph):
    seen = set()
    comps = []
    for start in nodes:
        if start in seen:
            continue
        comp = []
        stack = [start]
        seen.add(start)
        while stack:
            n = stack.pop()
            comp.append(n)
            for m in graph.get(n, ()):
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        comps.append(comp)
    return comps


def _component_chain(comp, graph, start):
    chain = []
    seen = set()
    stack = [start]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        chain.append(n)
        for m in sorted(graph.get(n, ())):
            if m in comp and m not in seen:
                stack.append(m)
    return chain


def _load_route_geometry_inputs(data):
    """Return per-route trip stop-sequences + stop graph from a static feed dict."""
    route_names = {rid: v.get("route_name", "") for rid, v in data["routes"].items()}

    stop_coords = {
        sid: (v["stop_lat"], v["stop_lon"])
        for sid, v in data["stops"].items()
        if v.get("stop_lat") is not None and v.get("stop_lon") is not None
    }

    by_trip = defaultdict(list)
    for (trip_id, stop_seq), st in data["stop_times"].items():
        by_trip[trip_id].append((stop_seq, st["stop_id"]))
    for trip_id, entries in by_trip.items():
        entries.sort(key=lambda e: e[0])

    trips_by_route = defaultdict(dict)
    for trip_id, trip in data["trips"].items():
        route_id = trip["route_id"]
        seq = [
            stop_id
            for _seq, stop_id in by_trip.get(trip_id, ())
            if stop_id in stop_coords
        ]
        if seq:
            trips_by_route[route_id].setdefault(trip_id, []).extend(seq)

    route_graphs = {}
    for route_id, trips in trips_by_route.items():
        graph = defaultdict(set)
        for seq in trips.values():
            for a, b in zip(seq, seq[1:]):
                if not _edge_ok(a, b, stop_coords):
                    continue
                graph[a].add(b)
                graph[b].add(a)
        route_graphs[route_id] = graph

    return route_names, stop_coords, trips_by_route, route_graphs


def _longest_clean_run(trips, graph):
    best = []
    for seq in trips:
        run = []
        for s in seq:
            if run and s in graph.get(run[-1], ()):
                run.append(s)
            else:
                run = [s]
            if len(run) > len(best):
                best = run
    return best


def _spider_order(graph, trips, stop_coords):
    """Order every stop on a route into one polyline.

    Walks the longest run of consecutive stops joined by real (post-pruning)
    edges as the spine, detouring out every off-spine branch (out-and-back DFS)
    so branch stops lie on the line too. Any component the main walk never
    reaches (isolated by skip-edge pruning) is walked as a coherent chain and
    spliced in right after the visited stop nearest to it, so chords are as
    short as the data allows.
    """
    all_stops = {s for seq in trips for s in seq}
    comps = _components(all_stops, graph)
    spine = _longest_clean_run(trips, graph)
    if len(spine) < 2 and trips:
        spine = max(trips, key=len)
    spine_set = set(spine)
    visited = set()
    order = []

    def detour(start):
        stack = [(start, iter(sorted(graph.get(start, ()))))]
        visited.add(start)
        order.append(start)
        while stack:
            node, children = stack[-1]
            entered = False
            for m in children:
                if m not in visited and m not in spine_set:
                    visited.add(m)
                    order.append(m)
                    stack.append((m, iter(sorted(graph.get(m, ())))))
                    entered = True
                    break
            if not entered:
                order.append(node)
                stack.pop()

    for s in spine:
        if s in visited:
            continue
        visited.add(s)
        order.append(s)
        for m in sorted(graph.get(s, ())):
            if m not in visited and m not in spine_set:
                detour(m)

    remaining = [c for c in comps if not set(c) & visited]
    for comp in remaining:
        anchor, attach = min(
            ((n, v) for n in comp for v in order),
            key=lambda nv: _dist2(nv[0], nv[1], stop_coords),
        )
        chain = _component_chain(comp, graph, anchor)
        idx = order.index(attach)
        order[idx + 1 : idx + 1] = chain
        visited.update(chain)

    return order


def build_geometries(data) -> list[dict]:
    """One polyline per bus/trolley route, sorted by route_id.

    Returns [{route_id, route_name, coordinates}] where coordinates is a
    [lat, lon] pair list, dropping routes with fewer than 2 stops (no line).
    """
    route_names, stop_coords, trips_by_route, route_graphs = _load_route_geometry_inputs(data)

    out = []
    for route_id in sorted(route_names):
        trips = list(trips_by_route.get(route_id, {}).values())
        if not trips:
            continue
        graph = route_graphs.get(route_id, {})
        coords = [
            list(stop_coords[stop_id])
            for stop_id in _spider_order(graph, trips, stop_coords)
            if stop_id in stop_coords
        ]
        if len(coords) < 2:
            continue
        out.append(
            {
                "route_id": route_id,
                "route_name": route_names[route_id],
                "coordinates": coords,
            }
        )
    return out
